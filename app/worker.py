import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select

from .config import settings
from .db import session_scope
from .log_service import write_log
from .models import Campaign, CampaignRecipient, CampaignStep, Contact, Message, OutboxJob, WhatsAppNumber
from .providers import ProviderError
from .providers.baileys import BaileysProvider, VercelBaileysProvider
from .providers.meta import MetaProvider
from .providers.uazapi import UazapiProvider
from .secret_store import decrypt_secret

MAX_ATTEMPTS = 4


async def _dispatch(job_id: str) -> None:
    with session_scope() as db:
        job = db.get(OutboxJob, job_id)
        if not job or job.status != "processing":
            return
        message = db.get(Message, job.message_id)
        contact = db.get(Contact, message.contact_id) if message else None
        number = db.get(WhatsAppNumber, message.phone_number_id) if message else None
        if not message or not contact or not number:
            job.status = "failed"
            job.last_error = "Referência de mensagem, contato ou canal ausente."
            return

        campaign = (
            db.get(Campaign, message.campaign_id)
            if message.campaign_id
            else None
        )

        # O job pode ter sido retirado da fila alguns instantes antes de o
        # usuário clicar em Pausar. Revalida o estado imediatamente antes do
        # envio para impedir que uma requisição antiga continue disparando.
        if campaign and campaign.status != "running":
            job.status = (
                "paused"
                if campaign.status == "paused"
                else "cancelled"
            )
            job.locked_at = None
            message.status = "queued"

            recipient = db.scalar(
                select(CampaignRecipient).where(
                    CampaignRecipient.campaign_id == message.campaign_id,
                    CampaignRecipient.contact_id == message.contact_id,
                )
            )

            if recipient:
                recipient.status = job.status
                recipient.reason = (
                    "Campanha pausada."
                    if job.status == "paused"
                    else "Campanha cancelada."
                )

            return

        if number.status != "connected":
            job.status = "failed"
            job.last_error = "O canal de WhatsApp está desconectado ou indisponível."
            if message:
                message.status = "failed"
                message.error_message = job.last_error
            recipient = (
                db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == message.campaign_id,
                        CampaignRecipient.contact_id == message.contact_id,
                    )
                )
                if message and message.campaign_id
                else None
            )
            if recipient:
                recipient.status = "failed"
                recipient.reason = job.last_error
            return

        try:
            token = decrypt_secret(number.access_token_encrypted or "")
            if number.provider == "meta_cloud":
                provider_id = await MetaProvider(token).send_text(number.phone_number_id, contact.phone_e164, message.body or "")
            elif number.provider == "uazapi":
                provider = UazapiProvider(instance_token=token)

                connection = UazapiProvider.connection(
                    await provider.status()
                )
                if not connection.connected:
                    raise ProviderError(
                        "A conexão UAZAPI não está conectada ao WhatsApp.",
                        "uazapi_not_connected",
                        False,
                    )

                if not await provider.check_number(contact.phone_e164):
                    raise ProviderError(
                        "O número do destinatário não está cadastrado no WhatsApp.",
                        "recipient_not_on_whatsapp",
                        False,
                    )

                provider_id = await provider.send_text(
                    contact.phone_e164,
                    message.body or "",
                )
            elif number.provider == "baileys":
                provider = (
                    VercelBaileysProvider(
                        settings.public_base_url,
                        settings.app_secret,
                        number.phone_number_id,
                    )
                    if number.waba_id == "vercel-internal"
                    else BaileysProvider(
                        number.waba_id or "",
                        token,
                        number.phone_number_id,
                    )
                )

                # O Baileys interno já valida o número dentro do próprio
                # endpoint de envio. Não abra um segundo socket só para check.
                if number.waba_id != "vercel-internal":
                    if not await provider.check_number(contact.phone_e164):
                        raise ProviderError(
                            "O número do destinatário não está cadastrado no WhatsApp.",
                            "recipient_not_on_whatsapp",
                            False,
                        )

                step = (
                    db.get(CampaignStep, message.campaign_step_id)
                    if message.campaign_step_id
                    else None
                )

                if message.type != "text":
                    if (
                        not step
                        or not step.media_type
                        or not step.media_mime
                        or not step.media_data_base64
                    ):
                        raise ProviderError(
                            "A mídia deste bloco não está disponível.",
                            "missing_campaign_media",
                            False,
                        )

                    provider_id = await provider.send_media(
                        contact.phone_e164,
                        message.body or "",
                        message.idempotency_key,
                        step.media_type,
                        step.media_mime,
                        step.media_filename or "arquivo",
                        step.media_data_base64,
                    )
                else:
                    provider_id = await provider.send_text(
                        contact.phone_e164,
                        message.body or "",
                        message.idempotency_key,
                    )
            else:
                raise ProviderError("Provedor de WhatsApp não suportado.", "unsupported_provider", False)
            message.provider_message_id = provider_id
            message.status = "sent"
            message.sent_at = datetime.now(timezone.utc)
            job.status = "sent"
            recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == message.campaign_id, CampaignRecipient.contact_id == message.contact_id))
            if recipient:
                recipient.status = "sent"
            write_log(db, job.workspace_id, "success", "dispatch", "message.sent", "Mensagem aceita pelo provedor.", provider=number.provider, campaign_id=message.campaign_id, contact_id=message.contact_id, message_id=message.id, details={"provider_message_id": provider_id, "attempt": job.attempts})
        except (ProviderError, Exception) as exc:
            retryable = getattr(exc, "retryable", True)
            job.last_error = str(exc)[:1000]
            message.error_message = job.last_error

            normalized_error = job.last_error.lower()

            baileys_auth_error = (
                number.provider == "baileys"
                and any(
                    marker in normalized_error
                    for marker in (
                        "pareada pelo qr",
                        "pareado pelo qr",
                        "sessão removida",
                        "sessao removida",
                        "logged out",
                        "loggedout",
                        "connection replaced",
                    )
                )
            )

            uazapi_auth_error = (
                number.provider == "uazapi"
                and (
                    getattr(exc, "code", "") in {
                        "401",
                        "invalid_stored_token",
                        "uazapi_not_connected",
                    }
                    or any(
                        marker in normalized_error
                        for marker in (
                            "unauthorized",
                            "no active session",
                            "não está conectada",
                            "nao esta conectada",
                            "token",
                        )
                    )
                )
            )

            baileys_busy = (
                number.provider == "baileys"
                and (
                    getattr(exc, "code", "") == "429"
                    or "sessão baileys está ocupada" in normalized_error
                    or "sessao baileys esta ocupada" in normalized_error
                    or "envio anterior" in normalized_error
                )
            )

            if baileys_busy:
                # Disputa interna entre chamadas serverless não deve consumir
                # tentativa do contato nem virar falha. Apenas devolve o job
                # para a fila por alguns segundos.
                job.status = "pending"
                job.attempts = max(0, (job.attempts or 1) - 1)
                job.locked_at = None
                job.available_at = datetime.now(timezone.utc) + timedelta(seconds=3)
                job.last_error = None
                message.status = "queued"
                message.error_message = None

                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == message.campaign_id,
                        CampaignRecipient.contact_id == message.contact_id,
                    )
                )

                if recipient:
                    recipient.status = "queued"
                    recipient.reason = None

                write_log(
                    db,
                    job.workspace_id,
                    "warning",
                    "dispatch",
                    "baileys.busy_retry",
                    "Sessão Baileys ocupada; envio devolvido para a fila.",
                    provider="baileys",
                    campaign_id=message.campaign_id,
                    contact_id=message.contact_id,
                    message_id=message.id,
                )
                return

            if uazapi_auth_error:
                campaign = (
                    db.get(Campaign, message.campaign_id)
                    if message.campaign_id
                    else None
                )

                number.status = "disconnected"

                if campaign:
                    campaign.status = "paused"

                job.status = "paused"
                job.available_at = datetime.now(timezone.utc) + timedelta(seconds=60)
                message.status = "queued"

                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == message.campaign_id,
                        CampaignRecipient.contact_id == message.contact_id,
                    )
                )

                if recipient:
                    recipient.status = "queued"
                    recipient.reason = (
                        "Conexão UAZAPI inválida ou desconectada. Reconecte o canal."
                    )

                write_log(
                    db,
                    job.workspace_id,
                    "warning",
                    "connection",
                    "uazapi.campaign_paused_auth",
                    "Campanha pausada porque a conexão UAZAPI precisa ser reconectada.",
                    provider="uazapi",
                    campaign_id=message.campaign_id,
                    contact_id=message.contact_id,
                    message_id=message.id,
                    details={"error": job.last_error},
                )

                return

            if baileys_auth_error:
                campaign = (
                    db.get(Campaign, message.campaign_id)
                    if message.campaign_id
                    else None
                )

                if campaign:
                    campaign.status = "paused"

                job.status = "paused"
                job.available_at = datetime.now(timezone.utc) + timedelta(seconds=60)

                message.status = "queued"

                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == message.campaign_id,
                        CampaignRecipient.contact_id == message.contact_id,
                    )
                )

                if recipient:
                    recipient.status = "queued"
                    recipient.reason = "Canal Baileys precisa ser reconectado."

                write_log(
                    db,
                    job.workspace_id,
                    "warning",
                    "connection",
                    "baileys.campaign_paused_auth",
                    "Campanha pausada porque a sessão Baileys precisa ser reconectada.",
                    provider="baileys",
                    campaign_id=message.campaign_id,
                    contact_id=message.contact_id,
                    message_id=message.id,
                    details={"error": job.last_error},
                )

                return

            if retryable and job.attempts < MAX_ATTEMPTS:
                job.status = "pending"
                job.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** job.attempts * 5))
                level, event = "warning", "message.retry_scheduled"
            else:
                job.status = "failed"
                message.status = "failed"
                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == message.campaign_id,
                        CampaignRecipient.contact_id == message.contact_id,
                    )
                )
                if recipient:
                    recipient.status = "failed"
                    recipient.reason = job.last_error[:160]
                level, event = "error", "message.failed"
            write_log(db, job.workspace_id, level, "dispatch", event, str(exc), provider=number.provider, campaign_id=message.campaign_id, contact_id=message.contact_id, message_id=message.id, details={"attempt": job.attempts, "retryable": retryable})


def _is_internal_baileys_campaign(
    workspace_id: str,
    campaign_id: str,
) -> bool:
    with session_scope() as db:
        campaign = db.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.workspace_id == workspace_id,
                Campaign.status == "running",
            )
        )

        if not campaign:
            return False

        number = db.get(
            WhatsAppNumber,
            campaign.phone_number_id,
        )

        return bool(
            number
            and number.provider == "baileys"
            and number.waba_id == "vercel-internal"
            and number.status == "connected"
        )


def _claim_baileys_batch(
    workspace_id: str,
    campaign_id: str,
    limit: int = 15,
) -> list[str]:
    with session_scope() as db:
        now_value = datetime.now(timezone.utc)
        stale_before = now_value - timedelta(seconds=75)

        stale_jobs = db.scalars(
            select(OutboxJob)
            .join(Message, Message.id == OutboxJob.message_id)
            .where(
                OutboxJob.workspace_id == workspace_id,
                Message.campaign_id == campaign_id,
                OutboxJob.status == "processing",
                OutboxJob.locked_at.is_not(None),
                OutboxJob.locked_at <= stale_before,
            )
            .limit(30)
        ).all()

        for stale_job in stale_jobs:
            stale_job.status = "pending"
            stale_job.locked_at = None
            stale_job.available_at = now_value
            stale_job.last_error = None

            stale_message = db.get(
                Message,
                stale_job.message_id,
            )

            if stale_message and stale_message.status not in {
                "sent",
                "delivered",
                "read",
            }:
                stale_message.status = "queued"
                stale_message.error_message = None

        query = (
            select(OutboxJob)
            .join(Message, Message.id == OutboxJob.message_id)
            .join(Campaign, Campaign.id == Message.campaign_id)
            .outerjoin(
                CampaignStep,
                CampaignStep.id == Message.campaign_step_id,
            )
            .where(
                OutboxJob.workspace_id == workspace_id,
                Message.campaign_id == campaign_id,
                Campaign.status == "running",
                OutboxJob.status == "pending",
                or_(
                    OutboxJob.available_at <= now_value,
                    CampaignStep.position == 1,
                ),
            )
            .order_by(
                CampaignStep.position,
                OutboxJob.available_at,
                OutboxJob.created_at,
            )
            .limit(max(1, min(limit, 15)))
            .with_for_update(skip_locked=True)
        )

        jobs = list(
            db.scalars(query).all()
        )

        for job in jobs:
            job.status = "processing"
            job.attempts += 1
            job.locked_at = now_value

        campaign = db.get(
            Campaign,
            campaign_id,
        )

        if campaign and campaign.processing_rate < settings.baileys_max_messages_per_minute:
            campaign.processing_rate = settings.baileys_max_messages_per_minute

        return [job.id for job in jobs]


async def _dispatch_baileys_batch(
    job_ids: list[str],
) -> int:
    if not job_ids:
        return 0

    with session_scope() as db:
        rows = []

        for job_id in job_ids:
            job = db.get(
                OutboxJob,
                job_id,
            )

            if not job or job.status != "processing":
                continue

            message = db.get(
                Message,
                job.message_id,
            )

            contact = (
                db.get(Contact, message.contact_id)
                if message
                else None
            )

            number = (
                db.get(WhatsAppNumber, message.phone_number_id)
                if message
                else None
            )

            campaign = (
                db.get(Campaign, message.campaign_id)
                if message and message.campaign_id
                else None
            )

            if (
                not message
                or not contact
                or not number
                or not campaign
            ):
                job.status = "failed"
                job.locked_at = None
                job.last_error = (
                    "Referência de mensagem, contato, campanha ou canal ausente."
                )
                continue

            if campaign.status != "running":
                job.status = (
                    "paused"
                    if campaign.status == "paused"
                    else "cancelled"
                )
                job.locked_at = None
                message.status = "queued"
                continue

            if (
                number.provider != "baileys"
                or number.waba_id != "vercel-internal"
                or number.status != "connected"
            ):
                job.status = "failed"
                job.locked_at = None
                job.last_error = (
                    "O canal Baileys interno está desconectado ou indisponível."
                )
                message.status = "failed"
                message.error_message = job.last_error
                continue

            step = (
                db.get(CampaignStep, message.campaign_step_id)
                if message.campaign_step_id
                else None
            )

            media = None

            if message.type != "text":
                if (
                    not step
                    or not step.media_type
                    or not step.media_mime
                    or not step.media_data_base64
                ):
                    job.status = "failed"
                    job.locked_at = None
                    job.last_error = (
                        "A mídia deste bloco não está disponível."
                    )
                    message.status = "failed"
                    message.error_message = job.last_error
                    continue

                media = {
                    "type": step.media_type,
                    "mime": step.media_mime,
                    "filename": step.media_filename or "arquivo",
                    "data": step.media_data_base64,
                }

            rows.append(
                {
                    "job": job,
                    "message": message,
                    "contact": contact,
                    "number": number,
                    "campaign": campaign,
                    "payload": {
                        "requestId": message.idempotency_key,
                        "to": contact.phone_e164.lstrip("+"),
                        "text": message.body or "",
                        **({"media": media} if media else {}),
                    },
                }
            )

        if not rows:
            return 0

        number = rows[0]["number"]
        provider = VercelBaileysProvider(
            settings.public_base_url,
            settings.app_secret,
            number.phone_number_id,
        )

        try:
            results = await provider.send_batch(
                [row["payload"] for row in rows]
            )
        except ProviderError as exc:
            normalized = str(exc).lower()
            auth_error = any(
                marker in normalized
                for marker in (
                    "pareada pelo qr",
                    "pareado pelo qr",
                    "sessão removida",
                    "sessao removida",
                    "logged out",
                    "loggedout",
                    "connection replaced",
                )
            )

            busy = (
                getattr(exc, "code", "") == "429"
                or "sessão baileys está ocupada" in normalized
                or "sessao baileys esta ocupada" in normalized
                or "envio anterior" in normalized
            )

            for row in rows:
                job = row["job"]
                message = row["message"]
                campaign = row["campaign"]

                if auth_error:
                    campaign.status = "paused"
                    job.status = "paused"
                    job.available_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=60)
                    )
                else:
                    job.status = "pending"
                    job.available_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=1 if busy else 5)
                    )
                    if busy:
                        job.attempts = max(
                            0,
                            (job.attempts or 1) - 1,
                        )

                job.locked_at = None
                job.last_error = None if busy else str(exc)[:1000]
                message.status = "queued"
                message.error_message = None if busy else job.last_error

            return 0

        by_request = {
            str(item.get("requestId")): item
            for item in results
            if isinstance(item, dict)
        }

        sent_count = 0

        for row in rows:
            job = row["job"]
            message = row["message"]
            contact = row["contact"]
            campaign = row["campaign"]
            result = by_request.get(
                message.idempotency_key
            )

            if result and result.get("ok"):
                provider_id = str(
                    result.get("id") or ""
                )

                message.provider_message_id = (
                    provider_id or None
                )
                message.status = "sent"
                message.sent_at = datetime.now(timezone.utc)
                message.error_message = None

                job.status = "sent"
                job.locked_at = None
                job.last_error = None

                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.contact_id == contact.id,
                    )
                )

                if recipient:
                    recipient.status = "sent"
                    recipient.reason = None

                sent_count += 1
                continue

            error_message = str(
                (result or {}).get("error")
                or "O Baileys não retornou o resultado deste envio."
            )[:1000]

            error_code = str(
                (result or {}).get("code")
                or "batch_send_failed"
            )

            permanent = (
                error_code == "recipient_not_on_whatsapp"
                or error_code == "422"
                or "não está cadastrado" in error_message.lower()
            )

            job.locked_at = None
            job.last_error = error_message
            message.error_message = error_message

            if permanent or job.attempts >= MAX_ATTEMPTS:
                job.status = "failed"
                message.status = "failed"

                recipient = db.scalar(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.contact_id == contact.id,
                    )
                )

                if recipient:
                    recipient.status = "failed"
                    recipient.reason = error_message[:160]
            else:
                job.status = "pending"
                job.available_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=3)
                )
                message.status = "queued"

        write_log(
            db,
            rows[0]["job"].workspace_id,
            "success" if sent_count else "warning",
            "dispatch",
            "baileys.batch_processed",
            f"Lote Baileys processado: {sent_count}/{len(rows)} enviado(s).",
            provider="baileys",
            campaign_id=rows[0]["campaign"].id,
            details={
                "batch_size": len(rows),
                "sent": sent_count,
                "failed_or_retry": len(rows) - sent_count,
            },
        )

        return len(rows)


def _next_job(
    workspace_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
) -> str:
    with session_scope() as db:
        # Recupera jobs que ficaram presos como processing quando uma função
        # serverless expirou. Isso evita campanhas eternamente em "running"
        # com todos os contatos pendentes.
        stale_before = datetime.now(timezone.utc) - timedelta(seconds=75)

        stale_query = (
            select(OutboxJob)
            .join(Message, Message.id == OutboxJob.message_id)
            .join(Campaign, Campaign.id == Message.campaign_id)
            .where(
                OutboxJob.status == "processing",
                OutboxJob.locked_at.is_not(None),
                OutboxJob.locked_at <= stale_before,
                Campaign.status == "running",
            )
        )

        if workspace_id:
            stale_query = stale_query.where(
                OutboxJob.workspace_id == workspace_id
            )

        if campaign_id:
            stale_query = stale_query.where(
                Campaign.id == campaign_id
            )

        stale_jobs = db.scalars(
            stale_query.limit(25)
        ).all()

        for stale_job in stale_jobs:
            stale_job.status = "pending"
            stale_job.locked_at = None
            stale_job.available_at = datetime.now(timezone.utc)
            stale_job.last_error = None

            stale_message = db.get(
                Message,
                stale_job.message_id,
            )

            if stale_message and stale_message.status not in {
                "sent",
                "delivered",
                "read",
            }:
                stale_message.status = "queued"
                stale_message.error_message = None

        query = (
            select(OutboxJob)
            .join(Message, Message.id == OutboxJob.message_id)
            .join(Campaign, Campaign.id == Message.campaign_id)
            .where(
                OutboxJob.status == "pending",
                OutboxJob.available_at <= datetime.now(timezone.utc),
                Campaign.status == "running",
            )
        )
        if workspace_id:
            query = query.where(OutboxJob.workspace_id == workspace_id)
        if campaign_id:
            query = query.where(Campaign.id == campaign_id)
        job = db.scalar(query.order_by(OutboxJob.available_at, OutboxJob.created_at).limit(1).with_for_update(skip_locked=True))
        if not job:
            return ""
        job.status = "processing"
        job.attempts += 1
        job.locked_at = datetime.now(timezone.utc)
        return job.id


def _complete_campaigns() -> None:
    with session_scope() as db:
        campaigns = db.scalars(select(Campaign).where(Campaign.status == "running")).all()
        for campaign in campaigns:
            pending = db.scalar(
                select(func.count(OutboxJob.id))
                .join(Message, Message.id == OutboxJob.message_id)
                .where(Message.campaign_id == campaign.id, OutboxJob.status.in_(["pending", "processing"]))
            ) or 0
            if pending == 0:
                failed = db.scalar(
                    select(func.count(OutboxJob.id))
                    .join(Message, Message.id == OutboxJob.message_id)
                    .where(Message.campaign_id == campaign.id, OutboxJob.status == "failed")
                ) or 0
                cancelled = db.scalar(
                    select(func.count(OutboxJob.id))
                    .join(Message, Message.id == OutboxJob.message_id)
                    .where(Message.campaign_id == campaign.id, OutboxJob.status == "cancelled")
                ) or 0

                if failed:
                    campaign.status = "failed"
                elif cancelled:
                    campaign.status = "cancelled"
                else:
                    campaign.status = "completed"

                campaign.completed_at = datetime.now(timezone.utc)

                if failed:
                    write_log(
                        db,
                        campaign.workspace_id,
                        "error",
                        "campaign",
                        "campaign.failed",
                        f"Campanha finalizada com {failed} falha(s).",
                        campaign_id=campaign.id,
                    )
                elif cancelled:
                    write_log(
                        db,
                        campaign.workspace_id,
                        "warning",
                        "campaign",
                        "campaign.cancelled_jobs",
                        f"Campanha possui {cancelled} envio(s) cancelado(s).",
                        campaign_id=campaign.id,
                    )
                else:
                    write_log(db, campaign.workspace_id, "success", "campaign", "campaign.completed", "Campanha concluída.", campaign_id=campaign.id)


async def process_available_jobs(
    workspace_id: str,
    campaign_id: Optional[str] = None,
    max_jobs: int = 1,
) -> int:
    """Processa fila normal ou um lote rápido do Baileys interno."""
    if (
        campaign_id
        and await asyncio.to_thread(
            _is_internal_baileys_campaign,
            workspace_id,
            campaign_id,
        )
    ):
        job_ids = await asyncio.to_thread(
            _claim_baileys_batch,
            workspace_id,
            campaign_id,
            max_jobs,
        )

        processed = await _dispatch_baileys_batch(
            job_ids
        )

        await asyncio.to_thread(
            _complete_campaigns
        )

        return processed

    processed = 0

    for _ in range(
        max(1, min(max_jobs, 10))
    ):
        job_id = await asyncio.to_thread(
            _next_job,
            workspace_id,
            campaign_id,
        )

        if not job_id:
            break

        await _dispatch(job_id)
        processed += 1

    await asyncio.to_thread(
        _complete_campaigns
    )

    return processed


async def run_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        job_id = await asyncio.to_thread(_next_job)
        if job_id:
            await _dispatch(job_id)
            await asyncio.to_thread(_complete_campaigns)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
