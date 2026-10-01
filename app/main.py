import asyncio
import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, init_db
from .event_processor import process_baileys_payload, process_meta_payload, process_uazapi_payload
from .importers import parse_contacts_file
from .log_service import write_log
from .models import (
    Campaign,
    CampaignRecipient,
    CampaignStep,
    Consent,
    Contact,
    ContactList,
    Conversation,
    ConversationMessage,
    ListContact,
    Membership,
    Message,
    OutboxJob,
    SystemLog,
    User,
    WebhookEvent,
    WhatsAppNumber,
    Workspace,
    now,
)
from .providers import ProviderError
from .providers.baileys import BaileysConnection, BaileysProvider, VercelBaileysProvider
from .providers.meta import MetaProvider
from .providers.uazapi import UazapiConnection, UazapiProvider
from .secret_store import decrypt_secret, encrypt_secret
from .security import (
    ANONYMOUS_CSRF_COOKIE,
    SESSION_COOKIE,
    hash_password,
    new_anonymous_csrf,
    new_session,
    read_session,
    verify_anonymous_csrf,
    verify_csrf,
    verify_password,
)
from .services import enqueue_campaign, normalize_phone, simulate_campaign
from .worker import process_available_jobs, run_worker

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
worker_stop = asyncio.Event()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    task = asyncio.create_task(run_worker(worker_stop)) if settings.run_worker else None
    try:
        yield
    finally:
        if task:
            worker_stop.set()
            await task


app = FastAPI(title="Nexo Flow Web", version="2.0.0", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _session(request: Request) -> Optional[dict]:
    return read_session(request.cookies.get(SESSION_COOKIE))


def require_auth(request: Request, db: Session):
    session = _session(request)
    if not session:
        raise HTTPException(status_code=401)
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == session.get("user_id"),
            Membership.workspace_id == session.get("workspace_id"),
        )
    )
    user = db.get(User, session.get("user_id"))
    workspace = db.get(Workspace, session.get("workspace_id"))
    if not membership or not user or not workspace or not user.is_active:
        raise HTTPException(status_code=401)
    return session, user, workspace


async def checked_form(request: Request, session: dict):
    form = await request.form()
    if not verify_csrf(session, str(form.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="Token CSRF inválido. Atualize a página e tente novamente.")
    return form


def page(request: Request, name: str, auth=None, **context):
    payload = {"request": request, "auth": (auth[1], auth[2]) if auth else None, **context}
    if auth:
        payload["csrf"] = auth[0]["csrf"]
    return templates.TemplateResponse(request=request, name=name, context=payload)


async def sync_uazapi_number(number: WhatsAppNumber) -> UazapiConnection:
    token = decrypt_secret(number.access_token_encrypted or "")
    if not token:
        number.status = "disconnected"
        raise ProviderError(
            "O token desta conexão UAZAPI não pôde ser recuperado. Reconecte ou crie uma nova instância.",
            "invalid_stored_token",
            False,
        )
    try:
        connection = UazapiProvider.connection(
            await UazapiProvider(instance_token=token).status()
        )
    except ProviderError:
        # Nunca mantenha um canal como "connected" depois de uma falha real de
        # autenticação/status. Isso evitava conexões antigas aparecerem em campanhas.
        number.status = "disconnected"
        raise
    if connection.status in {"connected", "connecting", "disconnected", "hibernated"}:
        number.status = connection.status
    else:
        number.status = "connected" if connection.connected else "disconnected"
    if connection.connected:
        number.status = "connected"
    if connection.phone:
        try:
            number.phone_e164 = normalize_phone(connection.phone)
        except ValueError:
            pass
    return connection


def baileys_provider(number: WhatsAppNumber):
    if number.waba_id == "vercel-internal":
        return VercelBaileysProvider(settings.public_base_url, settings.app_secret, number.phone_number_id)
    token = decrypt_secret(number.access_token_encrypted or "")
    if not token:
        raise ProviderError("O token cifrado deste gateway não pôde ser recuperado.", "invalid_stored_token", False)
    return BaileysProvider(number.waba_id or "", token, number.phone_number_id)


async def sync_baileys_number(number: WhatsAppNumber) -> BaileysConnection:
    connection = BaileysProvider.connection(await baileys_provider(number).status())
    number.status = "connected" if connection.connected else connection.status
    if connection.phone:
        try:
            number.phone_e164 = normalize_phone(connection.phone)
        except ValueError:
            pass
    return connection


def anonymous_page(request: Request, name: str, **context):
    token, signed = new_anonymous_csrf()
    response = page(request, name, csrf=token, **context)
    response.set_cookie(ANONYMOUS_CSRF_COOKIE, signed, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=60 * 60)
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse("/login", status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request=request, name="error.html", context={"request": request, "message": str(exc.detail)}, status_code=exc.status_code)


@app.get("/", include_in_schema=False)
def home(request: Request):
    return RedirectResponse("/dashboard" if _session(request) else "/login", status_code=303)


def registration_open(db: Session) -> bool:
    return settings.allow_registration or not (db.scalar(select(func.count(User.id))) or 0)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if not registration_open(db):
        raise HTTPException(403, "Novos cadastros estão desativados.")
    return anonymous_page(request, "register.html")


@app.post("/register")
async def register(request: Request, db: Session = Depends(get_db)):
    if not registration_open(db):
        raise HTTPException(403, "Novos cadastros estão desativados.")
    form = await request.form()
    if not verify_anonymous_csrf(request.cookies.get(ANONYMOUS_CSRF_COOKIE), str(form.get("csrf", ""))):
        raise HTTPException(403, "Token CSRF inválido. Atualize a página e tente novamente.")
    workspace_name = str(form.get("workspace_name", "")).strip()
    name = str(form.get("name", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    if len(workspace_name) < 2 or len(name) < 2 or "@" not in email or len(password) < 10:
        return anonymous_page(request, "register.html", error="Revise os campos. A senha precisa ter pelo menos 10 caracteres.")
    if db.scalar(select(User.id).where(User.email == email)):
        return anonymous_page(request, "register.html", error="Este e-mail já está cadastrado.")
    user = User(name=name, email=email, password_hash=hash_password(password))
    workspace = Workspace(name=workspace_name)
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(user_id=user.id, workspace_id=workspace.id, role="owner"))
    write_log(db, workspace.id, "success", "system", "workspace.created", "Workspace criado.")
    db.commit()
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(SESSION_COOKIE, new_session(user.id, workspace.id), httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.session_max_age)
    response.delete_cookie(ANONYMOUS_CSRF_COOKIE)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if _session(request):
        return RedirectResponse("/dashboard", status_code=303)
    return anonymous_page(request, "login.html", registration_open=registration_open(db))


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if not verify_anonymous_csrf(request.cookies.get(ANONYMOUS_CSRF_COOKIE), str(form.get("csrf", ""))):
        raise HTTPException(403, "Token CSRF inválido. Atualize a página e tente novamente.")
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        return anonymous_page(request, "login.html", error="E-mail ou senha inválidos.")
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at))
    if not membership:
        return anonymous_page(request, "login.html", error="Conta sem workspace ativo.")
    response = RedirectResponse("/dashboard", status_code=303)
    remember = bool(form.get("remember"))
    response.set_cookie(SESSION_COOKIE, new_session(user.id, membership.workspace_id), httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.session_max_age if remember else None)
    response.delete_cookie(ANONYMOUS_CSRF_COOKIE)
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    return page(request, "account.html", auth, success=request.query_params.get("success"), error=request.query_params.get("error"))


@app.post("/account/password")
async def account_password(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirmation = str(form.get("password_confirmation", ""))
    if not verify_password(current_password, auth[1].password_hash):
        return RedirectResponse("/account?error=current", status_code=303)
    if len(new_password) < 10 or new_password != confirmation:
        return RedirectResponse("/account?error=new", status_code=303)
    auth[1].password_hash = hash_password(new_password)
    write_log(db, auth[2].id, "success", "security", "account.password_changed", "Senha da conta alterada.")
    db.commit()
    return RedirectResponse("/account?success=password", status_code=303)


@app.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    workspace_id = auth[2].id

    def count(model, *criteria):
        return db.scalar(select(func.count(model.id)).where(*criteria)) or 0

    metrics = {
        "contacts": count(Contact, Contact.workspace_id == workspace_id),
        "campaigns": count(Campaign, Campaign.workspace_id == workspace_id),
        "sent": count(Message, Message.workspace_id == workspace_id, Message.status == "sent"),
        "delivered": count(Message, Message.workspace_id == workspace_id, Message.status == "delivered"),
        "read": count(Message, Message.workspace_id == workspace_id, Message.status == "read"),
        "replied": count(Message, Message.workspace_id == workspace_id, Message.direction == "inbound"),
        "failed": count(Message, Message.workspace_id == workspace_id, Message.status == "failed"),
        "numbers": count(
            WhatsAppNumber,
            WhatsAppNumber.workspace_id == workspace_id,
            WhatsAppNumber.status != "removed",
        ),
    }
    campaigns = db.scalars(select(Campaign).where(Campaign.workspace_id == workspace_id).order_by(Campaign.created_at.desc()).limit(8)).all()
    return page(request, "dashboard.html", auth, metrics=metrics, campaigns=campaigns)


def filtered_logs(db: Session, workspace_id: str, level: str = "", category: str = "", provider: str = "", limit: int = 250):
    query = select(SystemLog).where(SystemLog.workspace_id == workspace_id)
    if level:
        query = query.where(SystemLog.level == level)
    if category:
        query = query.where(SystemLog.category == category)
    if provider:
        query = query.where(SystemLog.provider == provider)
    return db.scalars(query.order_by(SystemLog.created_at.desc()).limit(min(limit, 500))).all()


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, level: str = "", category: str = "", provider: str = "", db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    entries = filtered_logs(db, auth[2].id, level, category, provider)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = {name: db.scalar(select(func.count(SystemLog.id)).where(SystemLog.workspace_id == auth[2].id, SystemLog.created_at >= since, *([] if name == "total" else [SystemLog.level == name]))) or 0 for name in ["total", "success", "warning", "error"]}
    return page(request, "logs.html", auth, entries=entries, counts=counts, level=level, category=category, provider=provider)


@app.get("/api/logs")
def logs_api(request: Request, level: str = "", category: str = "", provider: str = "", limit: int = 250, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    items = []
    for entry in filtered_logs(db, auth[2].id, level, category, provider, limit):
        try:
            details = json.loads(entry.details_json or "{}")
        except ValueError:
            details = {}
        items.append({"id": entry.id, "created_label": entry.created_at.astimezone().strftime("%d/%m/%Y %H:%M:%S"), "level": entry.level, "category": entry.category, "provider": entry.provider, "event": entry.event, "message": entry.message, "details": details})
    return {"items": items}


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    contacts = db.scalars(select(Contact).where(Contact.workspace_id == auth[2].id).order_by(Contact.created_at.desc())).unique().all()
    return page(request, "contacts.html", auth, contacts=contacts, error=request.query_params.get("error"))


@app.post("/contacts")
async def create_contact(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    try:
        phone = normalize_phone(str(form.get("phone", "")))
    except ValueError as exc:
        return page(request, "contacts.html", auth, contacts=db.scalars(select(Contact).where(Contact.workspace_id == auth[2].id)).all(), error=str(exc))
    if db.scalar(select(Contact.id).where(Contact.workspace_id == auth[2].id, Contact.phone_e164 == phone)):
        return page(request, "contacts.html", auth, contacts=db.scalars(select(Contact).where(Contact.workspace_id == auth[2].id)).all(), error="Este telefone já está cadastrado.")
    contact = Contact(workspace_id=auth[2].id, name=str(form.get("name", "")).strip(), phone_e164=phone, email=str(form.get("email", "")).strip() or None, company=str(form.get("company", "")).strip() or None, source=str(form.get("source", "Manual")))
    db.add(contact)
    db.flush()
    opted_in = bool(form.get("opt_in"))
    db.add(Consent(workspace_id=auth[2].id, contact_id=contact.id, channel="whatsapp", status="opted_in" if opted_in else "unknown", source="manual", granted_at=now() if opted_in else None))
    write_log(db, auth[2].id, "success", "contacts", "contact.created", "Contato criado.", contact_id=contact.id)
    db.commit()
    return RedirectResponse("/contacts?success=1", status_code=303)


@app.post("/contacts/import")
async def import_contacts(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])

    upload = form.get("spreadsheet")

    if not upload or not hasattr(upload, "read"):
        return RedirectResponse(
            "/contacts?error=Selecione+uma+planilha",
            status_code=303,
        )

    try:
        payload = await upload.read()

        rows = parse_contacts_file(
            upload.filename or "",
            payload,
            bool(form.get("all_have_opt_in")),
            settings.max_import_rows,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/contacts?error={str(exc).replace(' ', '+')}",
            status_code=303,
        )

    list_name = str(form.get("list_name", "")).strip()

    if not list_name:
        list_name = Path(upload.filename or "Importados").stem

    list_name = list_name[:160] or "Importados"

    created = 0
    updated = 0
    invalid = 0
    opted_in = 0
    duplicates = 0
    imported_contact_ids = []

    seen_phones = set()

    try:
        contact_list = db.scalar(
            select(ContactList).where(
                ContactList.workspace_id == auth[2].id,
                ContactList.name == list_name,
            )
        )

        if not contact_list:
            contact_list = ContactList(
                workspace_id=auth[2].id,
                name=list_name,
                description="Criada pela importação Excel/CSV",
            )
            db.add(contact_list)
            db.flush()

        for row in rows:
            try:
                phone = normalize_phone(row.phone)
            except ValueError:
                invalid += 1
                continue

            if phone in seen_phones:
                duplicates += 1
                continue

            seen_phones.add(phone)

            incoming_name = (row.name or "").strip()

            if incoming_name.startswith("Contato ") and row.company:
                incoming_name = row.company

            incoming_name = (
                incoming_name
                or row.company
                or f"Contato {created + updated + 1}"
            )[:160]

            incoming_email = (row.email or "").strip()[:320] or None
            incoming_company = (row.company or "").strip()[:160] or None

            contact = db.scalar(
                select(Contact).where(
                    Contact.workspace_id == auth[2].id,
                    Contact.phone_e164 == phone,
                )
            )

            if contact:
                updated += 1

                if incoming_name:
                    contact.name = incoming_name

                if incoming_email:
                    contact.email = incoming_email

                if incoming_company:
                    contact.company = incoming_company

            else:
                contact = Contact(
                    workspace_id=auth[2].id,
                    name=incoming_name,
                    phone_e164=phone,
                    email=incoming_email,
                    company=incoming_company,
                    source="Excel/CSV",
                )

                db.add(contact)
                db.flush()

                created += 1

            consent = db.scalar(
                select(Consent).where(
                    Consent.contact_id == contact.id,
                    Consent.channel == "whatsapp",
                )
            )

            if not consent:
                consent = Consent(
                    workspace_id=auth[2].id,
                    contact_id=contact.id,
                    channel="whatsapp",
                    status="unknown",
                    source="import",
                )

                db.add(consent)

                # Importante porque SessionLocal usa autoflush=False
                db.flush()

            if row.opt_in:
                consent.status = "opted_in"
                consent.granted_at = now()
                opted_in += 1

            association = db.scalar(
                select(ListContact.id).where(
                    ListContact.list_id == contact_list.id,
                    ListContact.contact_id == contact.id,
                )
            )

            if not association:
                db.add(
                    ListContact(
                        list_id=contact_list.id,
                        contact_id=contact.id,
                    )
                )

                # Faz a associação ficar visível antes da próxima linha.
                db.flush()

            if contact.id not in imported_contact_ids:
                imported_contact_ids.append(contact.id)

        write_log(
            db,
            auth[2].id,
            "success",
            "contacts",
            "contacts.imported",
            "Importação concluída.",
            details={
                "created": created,
                "updated": updated,
                "invalid": invalid,
                "duplicates": duplicates,
                "opted_in": opted_in,
                "filename": upload.filename or "",
                "list_id": contact_list.id,
                "list_name": contact_list.name,
                "contact_ids": imported_contact_ids,
            },
        )

        db.commit()

    except IntegrityError:
        db.rollback()

        return RedirectResponse(
            "/contacts?error=Não+foi+possível+importar+a+planilha.+Verifique+telefones+duplicados+ou+dados+inválidos.",
            status_code=303,
        )

    return RedirectResponse(
        f"/contacts?imported={created}&updated={updated}&invalid={invalid}&duplicates={duplicates}&opted_in={opted_in}",
        status_code=303,
    )


@app.get("/contacts/template.csv")
def contacts_template():
    return PlainTextResponse("nome;telefone;email;empresa;opt_in\nMaria Silva;+5511999999999;maria@empresa.com;Empresa Exemplo;sim\n", media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=modelo-contatos-nexo-flow.csv"})


@app.get("/lists", response_class=HTMLResponse)
def lists_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)

    contacts = db.scalars(
        select(Contact)
        .where(Contact.workspace_id == auth[2].id)
        .order_by(Contact.name)
    ).all()

    rows = db.execute(
        select(ContactList, func.count(ListContact.id))
        .outerjoin(ListContact, ListContact.list_id == ContactList.id)
        .where(ContactList.workspace_id == auth[2].id)
        .group_by(ContactList.id)
        .order_by(ContactList.created_at.desc())
    ).all()

    last_csv_ids = []
    last_csv_name = ""

    latest_import = db.scalar(
        select(SystemLog)
        .where(
            SystemLog.workspace_id == auth[2].id,
            SystemLog.event == "contacts.imported",
        )
        .order_by(SystemLog.created_at.desc())
        .limit(1)
    )

    if latest_import:
        try:
            details = json.loads(latest_import.details_json or "{}")
        except ValueError:
            details = {}

        raw_ids = details.get("contact_ids") or []
        if isinstance(raw_ids, list):
            allowed_ids = set(
                db.scalars(
                    select(Contact.id).where(
                        Contact.workspace_id == auth[2].id,
                        Contact.id.in_(raw_ids),
                    )
                ).all()
            )
            last_csv_ids = [
                contact_id
                for contact_id in raw_ids
                if contact_id in allowed_ids
            ]

        last_csv_name = str(
            details.get("filename")
            or details.get("list_name")
            or ""
        ).strip()

    # Compatibilidade com importações feitas antes de registrarmos os IDs
    # do lote: usa a lista de importação mais recente como fallback.
    if not last_csv_ids:
        latest_import_list = db.scalar(
            select(ContactList)
            .where(
                ContactList.workspace_id == auth[2].id,
                ContactList.description == "Criada pela importação Excel/CSV",
            )
            .order_by(ContactList.created_at.desc())
            .limit(1)
        )

        if latest_import_list:
            last_csv_ids = list(
                db.scalars(
                    select(ListContact.contact_id)
                    .join(Contact, Contact.id == ListContact.contact_id)
                    .where(ListContact.list_id == latest_import_list.id)
                    .order_by(Contact.created_at.desc())
                ).all()
            )
            last_csv_name = latest_import_list.name

    return page(
        request,
        "lists.html",
        auth,
        contacts=contacts,
        rows=rows,
        last_csv_ids=last_csv_ids,
        last_csv_name=last_csv_name,
    )


@app.post("/lists")
async def create_list(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    item = ContactList(workspace_id=auth[2].id, name=str(form.get("name", "")).strip(), description=str(form.get("description", "")).strip() or None)
    db.add(item)
    try:
        db.flush()
        for contact_id in form.getlist("contact_ids"):
            if db.scalar(select(Contact.id).where(Contact.id == contact_id, Contact.workspace_id == auth[2].id)):
                db.add(ListContact(list_id=item.id, contact_id=contact_id))
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/lists?error=duplicate", status_code=303)
    return RedirectResponse("/lists?success=1", status_code=303)


@app.get("/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    numbers = db.scalars(
        select(WhatsAppNumber)
        .where(
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.status != "removed",
        )
        .order_by(WhatsAppNumber.created_at.desc())
    ).all()
    sync_errors = []
    for number in numbers:
        if number.status == "disabled":
            continue
        try:
            if number.provider == "uazapi":
                await sync_uazapi_number(number)
            elif number.provider == "baileys":
                await sync_baileys_number(number)
            else:
                continue
        except ProviderError as exc:
            sync_errors.append(str(exc))
    db.commit()
    connected_uazapi = sum(number.provider == "uazapi" and number.status == "connected" for number in numbers)
    connected_baileys = sum(number.provider == "baileys" and number.status == "connected" for number in numbers)
    connected_meta = sum(number.provider == "meta_cloud" and number.status == "connected" for number in numbers)
    return page(
        request,
        "whatsapp.html",
        auth,
        numbers=numbers,
        settings=settings,
        error=request.query_params.get("error"),
        sync_errors=sync_errors,
        connected_uazapi=connected_uazapi,
        connected_baileys=connected_baileys,
        connected_meta=connected_meta,
    )


@app.post("/whatsapp/{number_id}/disconnect")
async def disconnect_whatsapp_number(
    number_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])

    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.status != "removed",
        )
    )

    if not number:
        raise HTTPException(404, "Canal não encontrado.")

    provider_error = None

    try:
        if number.provider == "uazapi":
            token = decrypt_secret(number.access_token_encrypted or "")
            if token:
                await UazapiProvider(instance_token=token).disconnect()

        elif (
            number.provider == "baileys"
            and number.waba_id == "vercel-internal"
        ):
            await baileys_provider(number).disconnect()

    except ProviderError as exc:
        # Mesmo se o provedor externo estiver indisponível, o usuário pode
        # desabilitar o canal dentro do Nexo Flow para impedir novos envios.
        provider_error = str(exc)

    running_campaigns = db.scalars(
        select(Campaign).where(
            Campaign.workspace_id == auth[2].id,
            Campaign.phone_number_id == number.id,
            Campaign.status == "running",
        )
    ).all()

    for campaign in running_campaigns:
        campaign.status = "paused"

    number.status = "disabled"

    write_log(
        db,
        auth[2].id,
        "warning" if provider_error else "info",
        "connection",
        "whatsapp.disconnected",
        (
            "Canal desativado no Nexo Flow; o provedor externo não confirmou a desconexão."
            if provider_error
            else "Canal desconectado pelo usuário."
        ),
        provider=number.provider,
        details={
            "number_id": number.id,
            "provider_error": provider_error,
            "paused_campaigns": len(running_campaigns),
        },
    )

    db.commit()

    if provider_error:
        return RedirectResponse(
            "/whatsapp?disconnected=local",
            status_code=303,
        )

    return RedirectResponse(
        "/whatsapp?disconnected=1",
        status_code=303,
    )


@app.post("/whatsapp/{number_id}/delete")
async def delete_whatsapp_number(
    number_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])

    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.status != "removed",
        )
    )

    if not number:
        raise HTTPException(404, "Canal não encontrado.")

    original_provider = number.provider
    original_name = number.display_name
    provider_error = None

    try:
        if number.provider == "uazapi":
            token = decrypt_secret(number.access_token_encrypted or "")
            if token:
                await UazapiProvider(instance_token=token).disconnect()

        elif (
            number.provider == "baileys"
            and number.waba_id == "vercel-internal"
        ):
            await baileys_provider(number).disconnect()

    except ProviderError as exc:
        # A remoção local continua para permitir limpar conexões quebradas.
        provider_error = str(exc)

    running_campaigns = db.scalars(
        select(Campaign).where(
            Campaign.workspace_id == auth[2].id,
            Campaign.phone_number_id == number.id,
            Campaign.status == "running",
        )
    ).all()

    for campaign in running_campaigns:
        campaign.status = "paused"

    # Remoção lógica: preserva campanhas, mensagens e conversas antigas,
    # mas retira o canal da interface e invalida suas credenciais.
    number.status = "removed"
    number.access_token_encrypted = None
    number.webhook_secret = None
    number.quality_rating = None
    number.waba_id = None
    number.phone_number_id = f"removed-{number.id}"

    write_log(
        db,
        auth[2].id,
        "warning" if provider_error else "info",
        "connection",
        "whatsapp.removed",
        (
            f'Canal "{original_name}" removido do Nexo Flow, mas o provedor externo não confirmou a desconexão.'
            if provider_error
            else f'Canal "{original_name}" removido do Nexo Flow.'
        ),
        provider=original_provider,
        details={
            "number_id": number.id,
            "paused_campaigns": len(running_campaigns),
            "provider_error": provider_error,
        },
    )

    db.commit()

    return RedirectResponse(
        "/whatsapp?deleted=1",
        status_code=303,
    )


@app.post("/whatsapp/connect")
async def connect_meta(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    token = str(form.get("access_token", "")) or settings.whatsapp_access_token
    phone_number_id = str(form.get("phone_number_id", "")).strip()
    try:
        data = await MetaProvider(token).verify_number(phone_number_id)
    except ProviderError as exc:
        write_log(db, auth[2].id, "error", "connection", "meta.connection_failed", str(exc), provider="meta_cloud")
        db.commit()
        return RedirectResponse(f"/whatsapp?error={str(exc).replace(' ', '+')}", status_code=303)
    number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.workspace_id == auth[2].id, WhatsAppNumber.provider == "meta_cloud", WhatsAppNumber.phone_number_id == phone_number_id))
    if not number:
        number = WhatsAppNumber(workspace_id=auth[2].id, provider="meta_cloud", phone_number_id=phone_number_id, waba_id=str(form.get("waba_id", "")).strip(), display_name=data.get("verified_name") or "WhatsApp Business")
        db.add(number)
    number.phone_e164 = data.get("display_phone_number")
    number.quality_rating = data.get("quality_rating")
    number.status = "connected"
    number.access_token_encrypted = encrypt_secret(token)
    write_log(db, auth[2].id, "success", "connection", "whatsapp.connected", "Canal oficial validado e conectado.", provider="meta_cloud")
    db.commit()
    return RedirectResponse("/whatsapp?success=meta", status_code=303)


@app.post("/whatsapp/connect/uazapi")
async def connect_uazapi(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    try:
        provider = UazapiProvider(admin_token=str(form.get("admin_token", "")))
        data = await provider.create_instance(str(form.get("instance_name", "")).strip())
        instance_id = str(data.get("instanceId") or data.get("instance", {}).get("id") or data.get("id") or "")
        token = str(data.get("token") or data.get("instance", {}).get("token") or "")
        if not instance_id or not token:
            raise ProviderError("A UAZAPI não retornou o ID e o token da nova instância.")
        number = WhatsAppNumber(workspace_id=auth[2].id, provider="uazapi", phone_number_id=instance_id, display_name=str(form.get("instance_name", "")).strip(), phone_e164=normalize_phone(str(form.get("phone"))) if form.get("phone") else None, status="pending", access_token_encrypted=encrypt_secret(token), webhook_secret=uuid4().hex)
        db.add(number)
        db.flush()
        write_log(db, auth[2].id, "success", "connection", "uazapi.instance_created", "Instância UAZAPI criada.", provider="uazapi")
        db.commit()
        try:
            connection = UazapiProvider.connection(await UazapiProvider(instance_token=token).connect(str(form.get("phone", "")) or None))
            number.status = "connected" if connection.connected else connection.status
            db.commit()
        except ProviderError as exc:
            number.status = "disconnected"
            write_log(db, auth[2].id, "warning", "connection", "uazapi.initial_connect_failed", str(exc), provider="uazapi")
            db.commit()
            return RedirectResponse(f"/whatsapp/uazapi/{number.id}?connection_error=1", status_code=303)
        return page(
            request,
            "uazapi_connect.html",
            auth,
            number=number,
            connection=connection,
            error=None,
            created=True,
            connection_started=True,
        )
    except (ProviderError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/whatsapp?error={str(exc).replace(' ', '+')}", status_code=303)


@app.get("/whatsapp/qr", response_class=HTMLResponse)
def whatsapp_qr(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    return page(request, "whatsapp_qr.html", auth)


@app.get("/whatsapp/uazapi/{number_id}", response_class=HTMLResponse)
async def uazapi_status(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.id == number_id, WhatsAppNumber.workspace_id == auth[2].id, WhatsAppNumber.provider == "uazapi"))
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    connection = None
    error = None
    try:
        connection = await sync_uazapi_number(number)
        db.commit()
    except ProviderError as exc:
        error = str(exc)
    return page(request, "uazapi_connect.html", auth, number=number, connection=connection, error=error, created=False, connection_started=False)


@app.get("/api/whatsapp/uazapi/{number_id}/status")
async def uazapi_status_api(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.provider == "uazapi",
        )
    )
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    try:
        connection = await sync_uazapi_number(number)
        db.commit()
    except ProviderError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {
            "status": connection.status,
            "connected": connection.connected,
            "logged_in": connection.logged_in,
            "qrcode": connection.qrcode,
            "paircode": connection.paircode,
            "phone": number.phone_e164,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/whatsapp/uazapi/{number_id}/connect")
async def uazapi_reconnect(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.id == number_id, WhatsAppNumber.workspace_id == auth[2].id, WhatsAppNumber.provider == "uazapi"))
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    try:
        data = await UazapiProvider(instance_token=decrypt_secret(number.access_token_encrypted or "")).connect(str(form.get("phone", "")) or None)
        connection = UazapiProvider.connection(data)
        number.status = "connected" if connection.connected else connection.status
        db.commit()
        return page(
            request,
            "uazapi_connect.html",
            auth,
            number=number,
            connection=connection,
            error=None,
            created=False,
            connection_started=True,
        )
    except ProviderError as exc:
        write_log(db, auth[2].id, "warning", "connection", "uazapi.reconnect_failed", str(exc), provider="uazapi")
        db.commit()
        return page(
            request,
            "uazapi_connect.html",
            auth,
            number=number,
            connection=None,
            error=str(exc),
            created=False,
            connection_started=False,
        )


@app.post("/whatsapp/connect/baileys")
async def connect_baileys(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    display_name = str(form.get("display_name", "")).strip() or "Baileys"
    session_id = f"nexo-{uuid4().hex[:16]}"
    try:
        provider = VercelBaileysProvider(settings.public_base_url, settings.app_secret, session_id)
    except ProviderError as exc:
        return RedirectResponse(f"/whatsapp?error={str(exc).replace(' ', '+')}", status_code=303)
    number = WhatsAppNumber(
        workspace_id=auth[2].id,
        provider="baileys",
        phone_number_id=session_id,
        waba_id="vercel-internal",
        display_name=display_name,
        status="pending",
        access_token_encrypted=encrypt_secret("vercel-internal-managed"),
        webhook_secret=uuid4().hex,
    )
    db.add(number)
    db.flush()
    write_log(db, auth[2].id, "info", "connection", "baileys.session_created", "Sessão Baileys adicionada.", provider="baileys")
    db.commit()
    try:
        connection = BaileysProvider.connection(await provider.create_session())
        number.status = "connected" if connection.connected else connection.status
        if connection.phone:
            number.phone_e164 = normalize_phone(connection.phone)
        db.commit()
        return page(
            request,
            "baileys_connect.html",
            auth,
            number=number,
            connection=connection,
            error=None,
            created=True,
            connection_started=True,
        )
    except (ProviderError, ValueError) as exc:
        number.status = "disconnected"
        write_log(db, auth[2].id, "error", "connection", "baileys.connection_failed", str(exc), provider="baileys")
        db.commit()
        return page(
            request,
            "baileys_connect.html",
            auth,
            number=number,
            connection=None,
            error=str(exc),
            created=True,
            connection_started=False,
        )


@app.get("/whatsapp/baileys/{number_id}", response_class=HTMLResponse)
async def baileys_status(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.provider == "baileys",
        )
    )
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    connection = None
    error = None
    try:
        connection = await sync_baileys_number(number)
        db.commit()
    except ProviderError as exc:
        error = str(exc)
    return page(
        request,
        "baileys_connect.html",
        auth,
        number=number,
        connection=connection,
        error=error,
        created=False,
        connection_started=False,
    )


@app.get("/api/whatsapp/baileys/{number_id}/status")
async def baileys_status_api(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.provider == "baileys",
        )
    )
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    try:
        connection = await sync_baileys_number(number)
        db.commit()
    except ProviderError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {
            "status": connection.status,
            "connected": connection.connected,
            "qrcode": connection.qrcode,
            "qrcode_svg": connection.qrcode_svg,
            "phone": number.phone_e164,
            "last_error": connection.last_error,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/whatsapp/baileys/{number_id}/connect")
async def baileys_reconnect(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])
    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == number_id,
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.provider == "baileys",
        )
    )
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    try:
        connection = BaileysProvider.connection(await baileys_provider(number).connect())
        number.status = "connected" if connection.connected else connection.status
        db.commit()
        return page(
            request,
            "baileys_connect.html",
            auth,
            number=number,
            connection=connection,
            error=None,
            created=False,
            connection_started=True,
        )
    except ProviderError as exc:
        write_log(db, auth[2].id, "warning", "connection", "baileys.reconnect_failed", str(exc), provider="baileys")
        db.commit()
        return page(
            request,
            "baileys_connect.html",
            auth,
            number=number,
            connection=None,
            error=str(exc),
            created=False,
            connection_started=False,
        )


@app.get("/whatsapp/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    numbers = db.scalars(
        select(WhatsAppNumber).where(
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.status.notin_(["disabled", "removed"]),
        )
    ).all()
    for number in numbers:
        try:
            if number.provider == "uazapi":
                await sync_uazapi_number(number)
            elif number.provider == "baileys":
                await sync_baileys_number(number)
        except ProviderError:
            pass
    db.commit()
    recent_events = db.scalars(select(WebhookEvent).where(WebhookEvent.workspace_id == auth[2].id).order_by(WebhookEvent.created_at.desc()).limit(30)).all()
    public_https = settings.public_base_url.startswith("https://")
    meta_callback = f"{settings.public_base_url}/api/webhooks/whatsapp"
    meta_ready = public_https and bool(settings.whatsapp_verify_token and settings.whatsapp_app_secret)
    return page(request, "webhooks.html", auth, settings=settings, numbers=numbers, recent_events=recent_events, public_https=public_https, meta_callback=meta_callback, meta_ready=meta_ready)


@app.post("/whatsapp/uazapi/{number_id}/webhook/configure")
async def configure_uazapi_webhook(number_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])
    if not settings.public_base_url.startswith("https://"):
        return RedirectResponse("/whatsapp/webhooks?error=public_url", status_code=303)
    number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.id == number_id, WhatsAppNumber.workspace_id == auth[2].id, WhatsAppNumber.provider == "uazapi"))
    if not number:
        raise HTTPException(404, "Canal não encontrado.")
    url = f"{settings.public_base_url}/api/webhooks/uazapi/{number.id}?secret={number.webhook_secret}"
    try:
        await UazapiProvider(instance_token=decrypt_secret(number.access_token_encrypted or "")).configure_webhook(url)
        write_log(db, auth[2].id, "success", "webhook", "uazapi.webhook_configured", "Webhook UAZAPI configurado.", provider="uazapi")
        db.commit()
        return RedirectResponse("/whatsapp/webhooks?success=uazapi", status_code=303)
    except ProviderError as exc:
        write_log(db, auth[2].id, "error", "webhook", "uazapi.webhook_configuration_failed", str(exc), provider="uazapi")
        db.commit()
        return RedirectResponse("/whatsapp/webhooks?error=uazapi", status_code=303)


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    campaigns = db.scalars(select(Campaign).where(Campaign.workspace_id == auth[2].id).order_by(Campaign.created_at.desc())).all()
    return page(request, "campaigns.html", auth, campaigns=campaigns)


@app.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_new_page(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    lists = db.scalars(select(ContactList).where(ContactList.workspace_id == auth[2].id).order_by(ContactList.name)).all()
    external_numbers = db.scalars(
        select(WhatsAppNumber).where(
            WhatsAppNumber.workspace_id == auth[2].id,
            WhatsAppNumber.provider.in_(["uazapi", "baileys"]),
            WhatsAppNumber.status.notin_(["disabled", "removed"]),
        )
    ).all()
    for number in external_numbers:
        try:
            if number.provider == "uazapi":
                await sync_uazapi_number(number)
            else:
                await sync_baileys_number(number)
        except ProviderError:
            pass
    db.commit()
    numbers = db.scalars(select(WhatsAppNumber).where(WhatsAppNumber.workspace_id == auth[2].id, WhatsAppNumber.status == "connected").order_by(WhatsAppNumber.display_name)).all()
    return page(request, "campaign_new.html", auth, lists=lists, numbers=numbers, settings=settings)


@app.post("/campaigns")
async def create_campaign(request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])

    contact_list = db.scalar(
        select(ContactList).where(
            ContactList.id == form.get("list_id"),
            ContactList.workspace_id == auth[2].id,
        )
    )

    number = db.scalar(
        select(WhatsAppNumber).where(
            WhatsAppNumber.id == form.get("phone_number_id"),
            WhatsAppNumber.workspace_id == auth[2].id,
        )
    )

    if not contact_list or not number:
        raise HTTPException(400, "Lista ou canal inválido.")

    if number.status != "connected":
        raise HTTPException(
            409,
            "Este canal não está conectado. Atualize a conexão antes de criar a campanha.",
        )

    try:
        rate = int(form.get("processing_rate", 20))
    except (TypeError, ValueError):
        rate = 20

    rate = max(1, min(rate, settings.max_messages_per_minute))

    raw_messages = [str(value).strip() for value in form.getlist("message_block")]

    if not raw_messages:
        legacy_message = str(form.get("message", "")).strip()
        if legacy_message:
            raw_messages = [legacy_message]

    raw_delays = form.getlist("block_delay")
    blocks = []

    allowed_media = {
        "image/jpeg": ("image", "JPG"),
        "image/png": ("image", "PNG"),
        "image/webp": ("image", "WEBP"),
        "video/mp4": ("video", "MP4"),
        "application/pdf": ("document", "PDF"),
    }
    max_media_bytes = 2_500_000

    def valid_media_signature(mime: str, data: bytes) -> bool:
        if mime == "image/jpeg":
            return data.startswith(b"\xff\xd8\xff")
        if mime == "image/png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if mime == "image/webp":
            return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        if mime == "video/mp4":
            return len(data) >= 12 and data[4:8] == b"ftyp"
        if mime == "application/pdf":
            return data.startswith(b"%PDF")
        return False

    for source_index, body in enumerate(raw_messages):
        upload = form.get(f"message_media_{source_index}")
        filename = str(getattr(upload, "filename", "") or "").strip()
        media_type = None
        media_mime = None
        media_filename = None
        media_data_base64 = None

        if filename:
            media_mime = str(getattr(upload, "content_type", "") or "").lower().strip()

            if media_mime not in allowed_media:
                raise HTTPException(
                    400,
                    "Formato de mídia não suportado. Use JPG, PNG, WEBP, MP4 ou PDF.",
                )

            media_data = await upload.read(max_media_bytes + 1)

            if not media_data:
                raise HTTPException(400, "O arquivo de mídia está vazio.")

            if len(media_data) > max_media_bytes:
                raise HTTPException(
                    400,
                    "A mídia pode ter no máximo 2,5 MB para envio seguro pelo Baileys/Vercel.",
                )

            if not valid_media_signature(media_mime, media_data):
                raise HTTPException(
                    400,
                    "O conteúdo do arquivo não corresponde ao formato informado.",
                )

            media_type = allowed_media[media_mime][0]
            media_filename = Path(filename).name[:220]
            media_data_base64 = base64.b64encode(media_data).decode("ascii")

        if not body and not media_type:
            continue

        if len(body) > 4096:
            raise HTTPException(400, "Cada bloco pode ter no máximo 4096 caracteres.")

        if len(blocks) >= 10:
            raise HTTPException(400, "Use no máximo 10 blocos de mensagem.")

        if not blocks:
            delay_seconds = 0
        else:
            try:
                raw_delay = raw_delays[source_index] if source_index < len(raw_delays) else 4
                delay_seconds = int(raw_delay)
            except (TypeError, ValueError):
                delay_seconds = 4

            delay_seconds = max(1, min(delay_seconds, 60))

        blocks.append(
            {
                "body": body,
                "delay_seconds": delay_seconds,
                "media_type": media_type,
                "media_mime": media_mime,
                "media_filename": media_filename,
                "media_data_base64": media_data_base64,
            }
        )

    if not blocks:
        raise HTTPException(400, "Adicione pelo menos texto ou mídia em um bloco.")

    if number.provider != "baileys" and any(block["media_type"] for block in blocks):
        raise HTTPException(
            400,
            "O envio de mídia desta versão está disponível somente para canais Baileys.",
        )

    campaign = Campaign(
        workspace_id=auth[2].id,
        list_id=contact_list.id,
        phone_number_id=number.id,
        name=str(form.get("name", "")).strip(),
        status="draft",
        processing_rate=rate,
    )

    db.add(campaign)
    db.flush()

    for position, block in enumerate(blocks, start=1):
        db.add(
            CampaignStep(
                campaign_id=campaign.id,
                position=position,
                body=block["body"],
                delay_seconds=block["delay_seconds"],
                media_type=block["media_type"],
                media_mime=block["media_mime"],
                media_filename=block["media_filename"],
                media_data_base64=block["media_data_base64"],
            )
        )

    write_log(
        db,
        auth[2].id,
        "success",
        "campaign",
        "campaign.created",
        f"Campanha criada com {len(blocks)} bloco(s) de mensagem.",
        campaign_id=campaign.id,
        details={
            "message_blocks": len(blocks),
            "media_blocks": sum(1 for block in blocks if block["media_type"]),
        },
    )

    db.commit()

    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


def get_campaign(db: Session, workspace_id: str, campaign_id: str) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace_id))
    if not campaign:
        raise HTTPException(404, "Campanha não encontrada.")
    return campaign


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)

    campaign = get_campaign(db, auth[2].id, campaign_id)

    steps = db.scalars(
        select(CampaignStep)
        .where(CampaignStep.campaign_id == campaign.id)
        .order_by(CampaignStep.position)
    ).all()

    contact_list = db.get(ContactList, campaign.list_id)
    number = db.get(WhatsAppNumber, campaign.phone_number_id)

    simulation = simulate_campaign(
        db,
        auth[2].id,
        campaign,
    )

    base_gap = max(
        1.5,
        60.0 / max(1, campaign.processing_rate),
    )

    block_seconds = sum(
        max(
            base_gap,
            float(step.delay_seconds or 4),
        )
        for step in steps[1:]
    )

    if number and number.provider == "baileys":
        average_lead_gap = base_gap * 1.15 + 1.6
        interval_mode = "Inteligente Baileys"
    else:
        average_lead_gap = base_gap
        interval_mode = "Ritmo padrão"

    estimated_seconds = simulation.eligible * (
        block_seconds + average_lead_gap
    )

    if number and number.provider == "baileys":
        estimated_seconds += (
            simulation.eligible // 10
        ) * 16

    estimated_minutes = (
        max(
            1,
            int((estimated_seconds + 59) // 60),
        )
        if simulation.eligible
        else 0
    )

    deliveries = db.execute(
        select(
            Message,
            OutboxJob,
            Contact,
        )
        .join(
            OutboxJob,
            OutboxJob.message_id == Message.id,
        )
        .join(
            Contact,
            Contact.id == Message.contact_id,
        )
        .where(
            Message.campaign_id == campaign.id
        )
        .order_by(
            Contact.name,
            Message.created_at,
        )
    ).all()

    groups = {}

    for message, job, contact in deliveries:
        if contact.id not in groups:
            groups[contact.id] = {
                "contact": contact,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "paused": 0,
                "processing": 0,
                "pending": 0,
                "cancelled": 0,
                "attempts": 0,
                "diagnostic": "",
                "status": "pending",
            }

        group = groups[contact.id]
        group["total"] += 1
        group["attempts"] = max(
            group["attempts"],
            job.attempts or 0,
        )

        if job.status == "sent":
            group["sent"] += 1
        elif job.status == "failed":
            group["failed"] += 1
        elif job.status == "paused":
            group["paused"] += 1
        elif job.status == "processing":
            group["processing"] += 1
        elif job.status == "cancelled":
            group["cancelled"] += 1
        else:
            group["pending"] += 1

        error = (
            message.error_message
            or job.last_error
            or ""
        )

        if error:
            group["diagnostic"] = error

    delivery_groups = list(groups.values())

    for group in delivery_groups:
        if group["failed"]:
            group["status"] = "failed"
        elif group["sent"] == group["total"] and group["total"]:
            group["status"] = "sent"
        elif group["cancelled"]:
            group["status"] = "cancelled"
        elif group["paused"]:
            group["status"] = "paused"
        elif group["processing"]:
            group["status"] = "processing"
        else:
            group["status"] = "pending"

        if not group["diagnostic"]:
            if group["sent"] == group["total"] and group["total"]:
                group["diagnostic"] = "Todos os blocos foram aceitos."
            elif group["sent"]:
                group["diagnostic"] = (
                    f'{group["sent"]}/{group["total"]} bloco(s) enviados.'
                )
            elif group["cancelled"]:
                group["diagnostic"] = "Envio cancelado. Pode ser reprocessado."
            else:
                group["diagnostic"] = "Aguardando processamento."

    failed_deliveries = sum(
        group["failed"]
        for group in delivery_groups
    )
    cancelled_deliveries = sum(
        group["cancelled"]
        for group in delivery_groups
    )

    return page(
        request,
        "campaign_detail.html",
        auth,
        campaign=campaign,
        step=steps[0] if steps else None,
        steps=steps,
        contact_list=contact_list,
        number=number,
        simulation=simulation,
        estimated_minutes=estimated_minutes,
        interval_mode=interval_mode,
        delivery_groups=delivery_groups,
        failed_deliveries=failed_deliveries,
        cancelled_deliveries=cancelled_deliveries,
    )


@app.get("/campaigns/{campaign_id}/steps/{step_id}/media")
def campaign_step_media(
    campaign_id: str,
    step_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = require_auth(request, db)

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    step = db.scalar(
        select(CampaignStep).where(
            CampaignStep.id == step_id,
            CampaignStep.campaign_id == campaign.id,
        )
    )

    if not step or not step.media_data_base64 or not step.media_mime:
        raise HTTPException(404, "Mídia não encontrada.")

    try:
        payload = base64.b64decode(step.media_data_base64, validate=True)
    except ValueError:
        raise HTTPException(500, "Mídia armazenada inválida.")

    headers = {
        "Cache-Control": "private, max-age=3600",
    }

    if step.media_type == "document":
        safe_name = (step.media_filename or "documento.pdf").replace('"', "")
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'

    return Response(
        content=payload,
        media_type=step.media_mime,
        headers=headers,
    )


@app.post("/campaigns/{campaign_id}/start")
async def campaign_start(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    if str(form.get("confirmation", "")).strip().upper() != "CONFIRMAR":
        raise HTTPException(
            400,
            "Confirmação inválida.",
        )

    wants_json = (
        "application/json"
        in request.headers.get("accept", "")
    )

    if campaign.status != "draft":
        error_message = (
            "Esta campanha já foi iniciada. Atualize a página antes de tentar novamente."
        )

        if wants_json:
            return JSONResponse(
                {
                    "ok": False,
                    "status": campaign.status,
                    "error": error_message,
                },
                status_code=409,
            )

        return RedirectResponse(
            f"/campaigns/{campaign.id}?error=Campanha+já+iniciada",
            status_code=303,
        )

    number = db.get(
        WhatsAppNumber,
        campaign.phone_number_id,
    )

    if not number or number.workspace_id != auth[2].id:
        raise HTTPException(400, "Canal remetente inválido.")

    if number.provider == "uazapi":
        try:
            connection = await sync_uazapi_number(number)
        except ProviderError as exc:
            db.commit()
            error_message = (
                "A conexão UAZAPI selecionada não está autorizada. "
                "Reconecte o canal ou escolha outra conexão válida."
            )
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    {
                        "ok": False,
                        "status": "draft",
                        "error": error_message,
                        "detail": str(exc),
                        "reconnect_url": f"/whatsapp/uazapi/{number.id}",
                    },
                    status_code=409,
                )
            return RedirectResponse(
                f"/campaigns/{campaign.id}?error=Reconecte+o+canal+UAZAPI",
                status_code=303,
            )

        if not connection.connected:
            number.status = "disconnected"
            db.commit()
            error_message = (
                "A conexão UAZAPI selecionada não está conectada ao WhatsApp."
            )
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    {
                        "ok": False,
                        "status": "draft",
                        "error": error_message,
                        "reconnect_url": f"/whatsapp/uazapi/{number.id}",
                    },
                    status_code=409,
                )
            return RedirectResponse(
                f"/campaigns/{campaign.id}?error=Canal+UAZAPI+desconectado",
                status_code=303,
            )

    created = enqueue_campaign(
        db,
        auth[2].id,
        campaign,
    )

    write_log(
        db,
        auth[2].id,
        "success",
        "campaign",
        "campaign.started",
        f"Campanha iniciada com {created} destinatários.",
        campaign_id=campaign.id,
    )

    db.commit()

    if "application/json" in request.headers.get("accept", ""):
        return {
            "ok": True,
            "status": campaign.status,
            "created": created,
        }

    return RedirectResponse(
        f"/campaigns/{campaign.id}?started={created}",
        status_code=303,
    )


@app.post("/api/campaigns/{campaign_id}/process")
async def campaign_process(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    processed = 0

    if campaign.status == "running":
        processed = await process_available_jobs(
            auth[2].id,
            campaign.id,
        )

    db.expire_all()

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    rows = db.execute(
        select(
            Message,
            OutboxJob,
        )
        .join(
            OutboxJob,
            OutboxJob.message_id == Message.id,
        )
        .where(
            Message.campaign_id == campaign.id
        )
        .order_by(Message.created_at)
    ).all()

    grouped = {}

    for message, job in rows:
        contact_id = message.contact_id

        if contact_id not in grouped:
            grouped[contact_id] = {
                "contact_id": contact_id,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "paused": 0,
                "processing": 0,
                "attempts": 0,
                "diagnostic": "",
            }

        item = grouped[contact_id]
        item["total"] += 1
        item["attempts"] = max(
            item["attempts"],
            job.attempts or 0,
        )

        if job.status == "sent":
            item["sent"] += 1
        elif job.status == "failed":
            item["failed"] += 1
        elif job.status == "paused":
            item["paused"] += 1
        elif job.status == "processing":
            item["processing"] += 1

        error = (
            message.error_message
            or job.last_error
            or ""
        )

        if error:
            item["diagnostic"] = error

    deliveries = []

    for item in grouped.values():
        if item["failed"]:
            status = "failed"
        elif item["sent"] == item["total"] and item["total"]:
            status = "sent"
        elif item["paused"]:
            status = "paused"
        elif item["processing"]:
            status = "processing"
        else:
            status = "pending"

        if not item["diagnostic"]:
            if status == "sent":
                item["diagnostic"] = "Todos os blocos foram aceitos."
            elif item["sent"]:
                item["diagnostic"] = (
                    f'{item["sent"]}/{item["total"]} bloco(s) enviados.'
                )
            else:
                item["diagnostic"] = "Aguardando processamento."

        item["status"] = status
        deliveries.append(item)

    return {
        "processed": processed,
        "status": campaign.status,
        "deliveries": deliveries,
    }


@app.post("/campaigns/{campaign_id}/retry-failed")
async def campaign_retry_failed(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])
    campaign = get_campaign(db, auth[2].id, campaign_id)
    rows = db.execute(
        select(OutboxJob, Message)
        .join(Message, Message.id == OutboxJob.message_id)
        .where(
            Message.campaign_id == campaign.id,
            OutboxJob.status.in_(["failed", "cancelled"]),
        )
    ).all()
    for job, message in rows:
        job.status = "pending"
        job.attempts = 0
        job.available_at = now()
        job.locked_at = None
        job.last_error = None
        message.status = "queued"
        message.error_message = None
        recipient = db.scalar(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id == message.contact_id,
            )
        )
        if recipient:
            recipient.status = "queued"
            recipient.reason = None
    if rows:
        campaign.status = "running"
        campaign.completed_at = None
        write_log(db, auth[2].id, "info", "campaign", "campaign.retry_failed", f"{len(rows)} envio(s) recolocado(s) na fila.", campaign_id=campaign.id)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}?retried={len(rows)}", status_code=303)


@app.post("/campaigns/{campaign_id}/contacts/{contact_id}")
async def campaign_update_recipient(campaign_id: str, contact_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    campaign = get_campaign(db, auth[2].id, campaign_id)
    contact = db.scalar(
        select(Contact)
        .join(Message, Message.contact_id == Contact.id)
        .where(Contact.id == contact_id, Contact.workspace_id == auth[2].id, Message.campaign_id == campaign.id)
    )
    if not contact:
        raise HTTPException(404, "Contato não encontrado nesta campanha.")
    try:
        phone = normalize_phone(str(form.get("phone", "")))
    except ValueError as exc:
        return RedirectResponse(f"/campaigns/{campaign.id}?error={str(exc).replace(' ', '+')}", status_code=303)
    duplicate = db.scalar(
        select(Contact.id).where(
            Contact.workspace_id == auth[2].id,
            Contact.phone_e164 == phone,
            Contact.id != contact.id,
        )
    )
    if duplicate:
        return RedirectResponse(f"/campaigns/{campaign.id}?error=Este+telefone+já+está+cadastrado", status_code=303)
    contact.phone_e164 = phone
    write_log(db, auth[2].id, "info", "contacts", "contact.phone_updated", "Telefone corrigido pela campanha.", campaign_id=campaign.id, contact_id=contact.id)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}?contact_updated=1", status_code=303)


@app.post("/campaigns/{campaign_id}/deduplicate")
async def campaign_deduplicate(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    rows = db.execute(
        select(
            Message,
            OutboxJob,
        )
        .join(
            OutboxJob,
            OutboxJob.message_id == Message.id,
        )
        .where(
            Message.campaign_id == campaign.id
        )
        .order_by(Message.created_at)
    ).all()

    seen = set()
    removed = 0
    protected = 0

    for message, job in rows:
        key = (
            message.contact_id,
            message.idempotency_key.rsplit(":step:", 1)[-1]
            if ":step:" in message.idempotency_key
            else (message.body or "").strip(),
        )

        if key not in seen:
            seen.add(key)
            continue

        if message.status in {"sent", "delivered", "read"}:
            protected += 1
            continue

        db.delete(job)
        db.delete(message)
        removed += 1

    db.commit()

    return {
        "ok": True,
        "removed": removed,
        "protected": protected,
    }


@app.post("/campaigns/{campaign_id}/{operation}")
async def campaign_control(campaign_id: str, operation: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    await checked_form(request, auth[0])

    campaign = get_campaign(
        db,
        auth[2].id,
        campaign_id,
    )

    wants_json = (
        "application/json"
        in request.headers.get("accept", "")
    )

    if operation not in {
        "pause",
        "resume",
        "cancel",
    }:
        raise HTTPException(404)

    number = db.get(
        WhatsAppNumber,
        campaign.phone_number_id,
    )

    if operation == "resume":
        if number and number.provider == "baileys":
            try:
                connection = BaileysProvider.connection(
                    await baileys_provider(number).status()
                )
            except ProviderError as exc:
                payload = {
                    "ok": False,
                    "status": "paused",
                    "error": str(exc),
                    "reconnect_url": f"/whatsapp/baileys/{number.id}",
                }

                if wants_json:
                    return JSONResponse(
                        payload,
                        status_code=502,
                    )

                return RedirectResponse(
                    f"/campaigns/{campaign.id}?error=Baileys+indisponível",
                    status_code=303,
                )

            number.status = (
                "connected"
                if connection.connected
                else connection.status
            )

            if connection.phone:
                try:
                    number.phone_e164 = normalize_phone(
                        connection.phone
                    )
                except ValueError:
                    pass

            if not connection.connected:
                campaign.status = "paused"
                db.commit()

                payload = {
                    "ok": False,
                    "status": "paused",
                    "error": "O Baileys não está conectado. Reconecte o WhatsApp antes de continuar a campanha.",
                    "reconnect_url": f"/whatsapp/baileys/{number.id}",
                }

                if wants_json:
                    return JSONResponse(
                        payload,
                        status_code=409,
                    )

                return RedirectResponse(
                    f"/campaigns/{campaign.id}?error=Reconecte+o+Baileys",
                    status_code=303,
                )

        campaign.status = "running"

        jobs = db.scalars(
            select(OutboxJob)
            .join(
                Message,
                Message.id == OutboxJob.message_id,
            )
            .where(
                Message.campaign_id == campaign.id,
                OutboxJob.status.in_(
                    ["pending", "paused"]
                ),
            )
            .order_by(
                OutboxJob.available_at,
                OutboxJob.created_at,
            )
        ).all()

        base_gap = max(
            1.5,
            60.0 / max(
                1,
                campaign.processing_rate,
            ),
        )

        cursor = 0.0

        for index, job in enumerate(jobs):
            was_paused = (
                job.status == "paused"
            )

            job.status = "pending"
            job.locked_at = None

            if was_paused:
                job.attempts = 0
                job.last_error = None

                message = db.get(
                    Message,
                    job.message_id,
                )

                if message and message.status not in {
                    "sent",
                    "delivered",
                    "read",
                }:
                    message.status = "queued"
                    message.error_message = None

            if index > 0:
                cursor += (
                    base_gap
                    + ((index * 7) % 9) / 10
                )

                if index % 10 == 0:
                    cursor += 12

            job.available_at = (
                now()
                + timedelta(seconds=cursor)
            )

    elif operation == "pause":
        campaign.status = "paused"

        jobs = db.scalars(
            select(OutboxJob)
            .join(
                Message,
                Message.id == OutboxJob.message_id,
            )
            .where(
                Message.campaign_id == campaign.id,
                OutboxJob.status == "pending",
            )
        ).all()

        for job in jobs:
            job.status = "paused"

    else:
        campaign.status = "cancelled"

        jobs = db.scalars(
            select(OutboxJob)
            .join(
                Message,
                Message.id == OutboxJob.message_id,
            )
            .where(
                Message.campaign_id == campaign.id,
                OutboxJob.status.in_(
                    ["pending", "paused"]
                ),
            )
        ).all()

        for job in jobs:
            job.status = "cancelled"

    write_log(
        db,
        auth[2].id,
        "info",
        "campaign",
        f"campaign.{operation}",
        f"Campanha {operation}.",
        campaign_id=campaign.id,
    )

    db.commit()

    payload = {
        "ok": True,
        "status": campaign.status,
    }

    if wants_json:
        return payload

    return RedirectResponse(
        f"/campaigns/{campaign.id}",
        status_code=303,
    )


@app.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, selected: str = "", db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.workspace_id == auth[2].id)
        .order_by(Conversation.last_message_at.desc())
    ).all()

    conversation_cards = []
    for conversation in conversations:
        item_contact = db.get(Contact, conversation.contact_id)
        last_message = db.scalar(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.desc())
        )
        conversation_cards.append(
            {
                "conversation": conversation,
                "contact": item_contact,
                "last_message": last_message,
            }
        )

    active = (
        db.scalar(
            select(Conversation).where(
                Conversation.id == selected,
                Conversation.workspace_id == auth[2].id,
            )
        )
        if selected
        else (conversations[0] if conversations else None)
    )
    contact = db.get(Contact, active.contact_id) if active else None
    messages = (
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == active.id)
            .order_by(ConversationMessage.created_at)
        )
        if active
        else []
    )
    return page(
        request,
        "conversations.html",
        auth,
        conversations=conversations,
        conversation_cards=conversation_cards,
        active=active,
        contact=contact,
        messages=list(messages),
    )


@app.post("/conversations/{conversation_id}/reply")
async def conversation_reply(conversation_id: str, request: Request, db: Session = Depends(get_db)):
    auth = require_auth(request, db)
    form = await checked_form(request, auth[0])
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.workspace_id == auth[2].id))
    if not conversation:
        raise HTTPException(404, "Conversa não encontrada.")
    message_id = uuid4().hex
    message = Message(workspace_id=auth[2].id, contact_id=conversation.contact_id, phone_number_id=conversation.phone_number_id, direction="outbound", type="text", body=str(form.get("body", "")).strip(), status="queued", idempotency_key=f"reply:{message_id}")
    db.add(message)
    db.flush()
    db.add(OutboxJob(workspace_id=auth[2].id, message_id=message.id, idempotency_key=message.idempotency_key, status="pending"))
    db.add(ConversationMessage(conversation_id=conversation.id, message_id=message.id, direction="outbound", type="text", body=message.body))
    conversation.last_message_at = now()
    write_log(db, auth[2].id, "info", "dispatch", "conversation.replied", "Resposta adicionada à fila.", message_id=message.id, contact_id=message.contact_id)
    db.commit()
    if not settings.run_worker:
        await process_available_jobs(auth[2].id)
    return RedirectResponse(f"/conversations?selected={conversation.id}", status_code=303)


@app.get("/api/webhooks/whatsapp")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and settings.whatsapp_verify_token and hmac.compare_digest(token or "", settings.whatsapp_verify_token):
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "Falha na verificação do webhook.")


@app.post("/api/webhooks/whatsapp")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    payload_bytes = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    if settings.whatsapp_app_secret:
        expected = "sha256=" + hmac.new(settings.whatsapp_app_secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(403, "Assinatura Meta inválida.")
    elif settings.is_production:
        raise HTTPException(503, "WHATSAPP_APP_SECRET não configurado.")
    try:
        process_meta_payload(db, json.loads(payload_bytes or b"{}"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"status": "accepted"}


@app.post("/api/webhooks/uazapi/{number_id}")
async def receive_uazapi_webhook(number_id: str, request: Request, secret: str = "", db: Session = Depends(get_db)):
    number = db.get(WhatsAppNumber, number_id)
    if not number or number.provider != "uazapi" or not number.webhook_secret or not hmac.compare_digest(secret, number.webhook_secret):
        raise HTTPException(403, "Webhook UAZAPI inválido.")
    process_uazapi_payload(db, number, await request.json())
    db.commit()
    return {"status": "accepted"}


@app.post("/api/webhooks/baileys/{number_id}")
async def receive_baileys_webhook(number_id: str, request: Request, db: Session = Depends(get_db)):
    number = db.get(WhatsAppNumber, number_id)
    secret = request.headers.get("x-baileys-webhook-secret", "")
    if not number or number.provider != "baileys" or not number.webhook_secret or not hmac.compare_digest(secret, number.webhook_secret):
        raise HTTPException(403, "Webhook Baileys inválido.")
    process_baileys_payload(db, number, await request.json())
    db.commit()
    return {"status": "accepted"}


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "version": app.version, "worker": settings.run_worker, "database": "postgresql" if settings.database_url.startswith("postgresql") else "sqlite"}
