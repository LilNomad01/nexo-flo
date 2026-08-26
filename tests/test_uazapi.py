import base64

import httpx
import pytest

from app.db import SessionLocal
from app.event_processor import process_uazapi_payload
from app.models import WhatsAppNumber, Workspace
from app.providers import ProviderError
from app.providers.uazapi import UazapiProvider


def test_nested_connection_status_from_uazapi_contract():
    connection = UazapiProvider.connection(
        {
            "instance": {"status": "connected", "qrcode": None},
            "status": {"connected": True, "loggedIn": True, "jid": {"user": "5512996671788", "server": "s.whatsapp.net"}},
        }
    )

    assert connection.status == "connected"
    assert connection.connected is True
    assert connection.logged_in is True
    assert connection.phone == "5512996671788"


def test_status_object_is_not_rendered_as_status_text():
    connection = UazapiProvider.connection(
        {"status": {"connected": False, "loggedIn": False, "jid": None, "resetting": False}}
    )

    assert connection.status == "disconnected"
    assert connection.connected is False


def test_base64_png_qrcode_is_normalized_to_data_url():
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"
    connection = UazapiProvider.connection({"instance": {"status": "connecting", "qrcode": base64.b64encode(png).decode()}})

    assert connection.qrcode == f"data:image/png;base64,{base64.b64encode(png).decode()}"


def test_raw_qrcode_payload_is_rendered_as_png():
    connection = UazapiProvider.connection({"instance": {"status": "connecting", "qrcode": "2@raw-whatsapp-qr-payload"}})

    assert connection.qrcode is not None
    assert connection.qrcode.startswith("data:image/png;base64,")
    rendered = base64.b64decode(connection.qrcode.split(",", 1)[1])
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")


def test_recipient_not_on_whatsapp_is_a_permanent_portuguese_error():
    response = httpx.Response(500, json={"error": "the number 5511999999999@s.whatsapp.net is not on WhatsApp"})

    with pytest.raises(ProviderError) as captured:
        UazapiProvider._decode(response)

    assert str(captured.value) == "O número do destinatário não está cadastrado no WhatsApp."
    assert captured.value.code == "recipient_not_on_whatsapp"
    assert captured.value.retryable is False


def test_connection_webhook_updates_channel_without_creating_message():
    with SessionLocal() as db:
        workspace = Workspace(name="Webhook UAZAPI")
        db.add(workspace)
        db.flush()
        number = WhatsAppNumber(
            workspace_id=workspace.id,
            provider="uazapi",
            phone_number_id="instance-test",
            display_name="UAZAPI Teste",
            status="connecting",
        )
        db.add(number)
        db.flush()

        process_uazapi_payload(
            db,
            number,
            {
                "EventType": "connection",
                "instance": {"status": "connected"},
                "status": {"connected": True, "loggedIn": True, "jid": {"user": "5512996671788"}},
            },
        )

        assert number.status == "connected"
        assert number.phone_e164 == "+5512996671788"
        db.rollback()
