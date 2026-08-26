import hashlib
import hmac
import json
import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Campaign, Contact, ContactList, ListContact, Message, OutboxJob, WhatsAppNumber, Workspace
from app.security import hash_password, verify_password


def register(client):
    page = client.get("/register")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/register",
        data={
            "workspace_name": "Nexo Teste",
            "name": "Pessoa Teste",
            "email": "teste@nexoflow.local",
            "password": "senha-segura-123",
            "csrf": token,
        },
        follow_redirects=False,
    )
    assert response.status_code in (303, 200)


def csrf(client, path="/dashboard"):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_health_and_login_page(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert client.get("/login").status_code == 200


def test_password_hashing():
    encoded = hash_password("uma-senha-bem-segura")
    assert "uma-senha-bem-segura" not in encoded
    assert verify_password("uma-senha-bem-segura", encoded)
    assert not verify_password("senha-errada", encoded)


def test_registration_contact_and_list(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/contacts",
        data={"csrf": token, "name": "Maria Silva", "phone": "11 99999-9999", "email": "maria@example.com", "source": "Manual", "opt_in": "on"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Maria Silva" in response.text
    assert "+5511999999999" in response.text
    token = csrf(client, "/lists")
    response = client.post("/lists", data={"csrf": token, "name": "Clientes", "description": "Lista de teste"}, follow_redirects=True)
    assert response.status_code == 200
    assert "Clientes" in response.text


def test_campaign_queue_and_signed_meta_webhook(client):
    token = csrf(client, "/campaigns/new")
    with SessionLocal() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.name == "Nexo Teste"))
        contact = db.scalar(select(Contact).where(Contact.workspace_id == workspace.id))
        contact_list = db.scalar(select(ContactList).where(ContactList.workspace_id == workspace.id, ContactList.name == "Clientes"))
        db.add(ListContact(list_id=contact_list.id, contact_id=contact.id))
        number = WhatsAppNumber(
            workspace_id=workspace.id,
            provider="meta_cloud",
            phone_number_id="phone-test-123",
            waba_id="waba-test",
            display_name="Meta Teste",
            phone_e164="+5511888888888",
            status="connected",
        )
        db.add(number)
        db.commit()
        list_id, number_id = contact_list.id, number.id
    created = client.post(
        "/campaigns",
        data={"csrf": token, "name": "Campanha Teste", "list_id": list_id, "phone_number_id": number_id, "message": "Olá, {{nome}}!", "processing_rate": "20"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    location = created.headers["location"]
    detail = client.get(location)
    assert detail.status_code == 200
    assert "Campanha Teste" in detail.text
    assert "Olá, {{nome}}!" in detail.text
    token = csrf(client, location)
    started = client.post(f"{location}/start", data={"csrf": token, "confirmation": "CONFIRMAR"}, follow_redirects=False)
    assert started.status_code == 303
    with SessionLocal() as db:
        campaign = db.scalar(select(Campaign).where(Campaign.name == "Campanha Teste"))
        message = db.scalar(select(Message).where(Message.campaign_id == campaign.id))
        job = db.scalar(select(OutboxJob).where(OutboxJob.message_id == message.id))
        assert campaign.status == "running"
        assert message.body == "Olá, Maria Silva!"
        assert job.status == "pending"

    payload = {
        "entry": [{
            "id": "waba-test",
            "changes": [{"value": {"metadata": {"phone_number_id": "phone-test-123"}, "messages": [{"id": "wamid.inbound-test", "from": "5511999999999", "type": "text", "text": {"body": "Olá, preciso de ajuda"}}]}}],
        }]
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret-test", raw, hashlib.sha256).hexdigest()
    webhook = client.post("/api/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert webhook.status_code == 200
    inbox = client.get("/conversations")
    assert inbox.status_code == 200
    assert "Olá, preciso de ajuda" in inbox.text

    with SessionLocal() as db:
        campaign = db.scalar(select(Campaign).where(Campaign.name == "Campanha Teste"))
        message = db.scalar(select(Message).where(Message.campaign_id == campaign.id))
        job = db.scalar(select(OutboxJob).where(OutboxJob.message_id == message.id))
        campaign.status = "completed"
        message.status = "failed"
        message.error_message = "O número do destinatário não está cadastrado no WhatsApp."
        job.status = "failed"
        job.attempts = 4
        job.last_error = message.error_message
        db.commit()

    failed_page = client.get(location)
    assert failed_page.status_code == 200
    assert "A campanha terminou com falha" in failed_page.text
    assert "Tentar falhas novamente" in failed_page.text
    assert "não está cadastrado no WhatsApp" in failed_page.text

    token = csrf(client, location)
    updated = client.post(
        f"{location}/contacts/{contact.id}",
        data={"csrf": token, "phone": "+55 11 98888-7777"},
        follow_redirects=False,
    )
    assert updated.status_code == 303

    token = csrf(client, location)
    retried = client.post(f"{location}/retry-failed", data={"csrf": token}, follow_redirects=False)
    assert retried.status_code == 303
    with SessionLocal() as db:
        campaign = db.scalar(select(Campaign).where(Campaign.name == "Campanha Teste"))
        message = db.scalar(select(Message).where(Message.campaign_id == campaign.id))
        job = db.scalar(select(OutboxJob).where(OutboxJob.message_id == message.id))
        saved_contact = db.get(Contact, message.contact_id)
        assert saved_contact.phone_e164 == "+5511988887777"
        assert campaign.status == "running"
        assert message.status == "queued"
        assert job.status == "pending"
        assert job.attempts == 0


def test_protected_routes_and_webhook_verification(client):
    client.cookies.clear()
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    denied = client.get("/api/logs")
    assert denied.status_code == 401
    verified = client.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-test", "hub.challenge": "12345"})
    assert verified.status_code == 200
    assert verified.text == "12345"
