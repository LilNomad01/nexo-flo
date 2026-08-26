import base64

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.event_processor import process_baileys_payload
from app.models import Message, WhatsAppNumber, Workspace
from app.providers import ProviderError
from app.providers.baileys import BaileysProvider


def test_baileys_connection_normalizes_qr_and_phone():
    connection = BaileysProvider.connection(
        {"status": "connecting", "connected": False, "qrcode": "2@baileys-qr-payload", "phone": "5511999999999"}
    )

    assert connection.status == "connecting"
    assert connection.phone == "5511999999999"
    assert connection.qrcode is not None
    assert connection.qrcode.startswith("data:image/png;base64,")
    assert base64.b64decode(connection.qrcode.split(",", 1)[1]).startswith(b"\x89PNG")
    assert connection.qrcode_svg is not None
    assert connection.qrcode_svg.lstrip().startswith("<svg")
    assert "<path" in connection.qrcode_svg


def test_baileys_connection_hides_raw_timeout_error():
    connection = BaileysProvider.connection({"status": "disconnected", "lastError": "Error: timeout"})

    assert connection.last_error == "O QR expirou. Gere uma nova conexão."


def test_baileys_provider_requires_public_https_gateway():
    with pytest.raises(ProviderError):
        BaileysProvider("http://localhost:8080", "token", "session-test")
    with pytest.raises(ProviderError):
        BaileysProvider("https://127.0.0.1", "token", "session-test")


def test_baileys_recipient_error_is_not_retryable():
    response = httpx.Response(422, json={"error": "O número do destinatário não está cadastrado no WhatsApp."})

    with pytest.raises(ProviderError) as captured:
        BaileysProvider._decode(response)

    assert captured.value.retryable is False


def test_baileys_webhook_updates_connection_and_stores_inbound_message():
    with SessionLocal() as db:
        workspace = Workspace(name="Webhook Baileys")
        db.add(workspace)
        db.flush()
        number = WhatsAppNumber(
            workspace_id=workspace.id,
            provider="baileys",
            phone_number_id="session-test",
            waba_id="https://baileys.example.com",
            display_name="Baileys Teste",
            status="connecting",
        )
        db.add(number)
        db.flush()

        process_baileys_payload(
            db,
            number,
            {"event": "connection", "data": {"status": "connected", "connected": True, "phone": "5511888888888"}},
        )
        process_baileys_payload(
            db,
            number,
            {"event": "message", "data": {"id": "BAILEYS-IN-1", "from": "5511999999999", "text": "Olá pelo Baileys", "type": "conversation"}},
        )

        message = db.scalar(select(Message).where(Message.provider_message_id == "BAILEYS-IN-1"))
        assert number.status == "connected"
        assert number.phone_e164 == "+5511888888888"
        assert message is not None
        assert message.body == "Olá pelo Baileys"
        assert message.direction == "inbound"
        db.rollback()
