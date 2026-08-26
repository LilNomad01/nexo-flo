import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .log_service import write_log
from .models import Consent, Contact, Conversation, ConversationMessage, Message, WebhookEvent, WhatsAppNumber
from .providers.uazapi import UazapiProvider
from .services import normalize_phone

STOP_PATTERN = re.compile(r"\s*(PARAR|SAIR|CANCELAR|REMOVER)\s*", re.IGNORECASE)


def _payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _store_event(db: Session, workspace_id: Optional[str], provider: str, external_id: str, event_type: str, payload: dict) -> bool:
    if db.scalar(select(WebhookEvent.id).where(WebhookEvent.provider == provider, WebhookEvent.external_id == external_id)):
        return False
    db.add(
        WebhookEvent(
            workspace_id=workspace_id,
            provider=provider,
            external_id=external_id,
            event_type=event_type,
            payload_hash=_payload_hash(payload),
            payload_json=json.dumps(payload, ensure_ascii=False),
            processed_at=datetime.now(timezone.utc),
        )
    )
    return True


def _contact(db: Session, workspace_id: str, phone: str, name: str = "Mensagem recebida") -> Contact:
    phone_e164 = normalize_phone(phone)
    contact = db.scalar(select(Contact).where(Contact.workspace_id == workspace_id, Contact.phone_e164 == phone_e164))
    if contact:
        return contact
    contact = Contact(workspace_id=workspace_id, name=name or "Mensagem recebida", phone_e164=phone_e164, source="WhatsApp")
    db.add(contact)
    db.flush()
    db.add(Consent(workspace_id=workspace_id, contact_id=contact.id, channel="whatsapp", status="unknown", source="inbound"))
    return contact


def _inbound(db: Session, number: WhatsAppNumber, external_id: str, phone: str, body: str, message_type: str = "text") -> Message:
    existing = db.scalar(select(Message).where(Message.provider_message_id == external_id))
    if existing:
        return existing
    contact = _contact(db, number.workspace_id, phone)
    message = Message(
        workspace_id=number.workspace_id,
        contact_id=contact.id,
        phone_number_id=number.id,
        direction="inbound",
        type=message_type,
        body=body,
        status="received",
        provider_message_id=external_id,
        idempotency_key=f"inbound:{number.provider}:{external_id}",
    )
    db.add(message)
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == number.workspace_id,
            Conversation.contact_id == contact.id,
            Conversation.phone_number_id == number.id,
        )
    )
    if not conversation:
        conversation = Conversation(workspace_id=number.workspace_id, contact_id=contact.id, phone_number_id=number.id)
        db.add(conversation)
        db.flush()
    conversation.last_message_at = datetime.now(timezone.utc)
    db.flush()
    db.add(ConversationMessage(conversation_id=conversation.id, message_id=message.id, direction="inbound", type=message_type, body=body))
    if STOP_PATTERN.fullmatch(body or ""):
        consent = db.scalar(select(Consent).where(Consent.contact_id == contact.id, Consent.channel == "whatsapp"))
        if consent:
            consent.status = "opted_out"
            consent.revoked_at = datetime.now(timezone.utc)
        contact.suppressed = True
        write_log(db, number.workspace_id, "warning", "contacts", "contact.opted_out", "Contato solicitou remoção por mensagem.", provider=number.provider, contact_id=contact.id)
    return message


def _update_status(db: Session, provider_id: str, state: str) -> Optional[Message]:
    message = db.scalar(select(Message).where(Message.provider_message_id == provider_id))
    if message and state in {"sent", "delivered", "read", "failed"}:
        message.status = state
    return message


def process_meta_payload(db: Session, payload: dict) -> None:
    for entry in payload.get("entry", []):
        waba_id = str(entry.get("id", ""))
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.provider == "meta_cloud", WhatsAppNumber.phone_number_id == str(metadata.get("phone_number_id", ""))))
            if not number and waba_id:
                number = db.scalar(select(WhatsAppNumber).where(WhatsAppNumber.provider == "meta_cloud", WhatsAppNumber.waba_id == waba_id))
            if not number:
                continue
            for status in value.get("statuses", []):
                external_id = f"status:{status.get('id')}:{status.get('status')}:{status.get('timestamp', '')}"
                if not _store_event(db, number.workspace_id, "meta_cloud", external_id, "message.status", status):
                    continue
                message = _update_status(db, str(status.get("id", "")), str(status.get("status", "")))
                write_log(db, number.workspace_id, "error" if status.get("status") == "failed" else "success", "delivery", "meta.message_status", f"Status Meta atualizado para {status.get('status')}", provider="meta_cloud", message_id=message.id if message else None, details=status)
            for incoming in value.get("messages", []):
                external_id = str(incoming.get("id", _payload_hash(incoming)))
                if not _store_event(db, number.workspace_id, "meta_cloud", external_id, "message.received", incoming):
                    continue
                body = incoming.get("text", {}).get("body") or f"[{incoming.get('type', 'evento')}]"
                message = _inbound(db, number, external_id, str(incoming.get("from", "")), body, str(incoming.get("type", "text")))
                write_log(db, number.workspace_id, "success", "webhook", "meta.message_received", f"Mensagem recebida via Meta de {incoming.get('from', '')}", provider="meta_cloud", message_id=message.id)


def process_uazapi_payload(db: Session, number: WhatsAppNumber, payload: dict) -> None:
    event_type = str(payload.get("EventType") or payload.get("eventType") or payload.get("event") or payload.get("type") or "event")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    identity = str(data.get("messageid") or data.get("messageId") or data.get("id") or _payload_hash(payload))
    external_id = f"{number.id}:{event_type}:{identity}"
    if not _store_event(db, number.workspace_id, "uazapi", external_id, event_type, payload):
        return
    lowered = event_type.lower()
    if "connection" in lowered:
        connection = UazapiProvider.connection(data)
        state = connection.status
        if connection.connected:
            number.status = "connected"
        elif state in {"disconnected", "connecting", "hibernated"}:
            number.status = state
        if connection.phone:
            try:
                number.phone_e164 = normalize_phone(connection.phone)
            except ValueError:
                pass
        write_log(db, number.workspace_id, "info", "connection", "uazapi.connection", f"Evento de conexão UAZAPI recebido: {state}", provider="uazapi", details=payload)
        return
    if "update" in lowered or lowered in {"status", "message_status"}:
        state = str(data.get("status") or data.get("state") or "").lower()
        message = _update_status(db, identity, state)
        write_log(db, number.workspace_id, "error" if state == "failed" else "success", "delivery", "uazapi.message_status", f"Status UAZAPI atualizado para {state}", provider="uazapi", message_id=message.id if message else None, details=payload)
        return
    if "message" not in lowered:
        write_log(db, number.workspace_id, "info", "webhook", "uazapi.event_ignored", f"Evento UAZAPI sem processador específico: {event_type}", provider="uazapi", details={"event_type": event_type})
        return
    from_me = bool(data.get("wasSentByApi") or data.get("fromMe"))
    is_group = bool(data.get("isGroup")) or str(data.get("chatid", "")).endswith("@g.us")
    if from_me or is_group:
        return
    phone_value = data.get("sender_pn") or data.get("from") or data.get("sender") or data.get("chatid") or ""
    if isinstance(phone_value, dict):
        phone_value = phone_value.get("user") or phone_value.get("id") or ""
    phone = str(phone_value).split("@", 1)[0]
    content = data.get("content")
    if isinstance(content, dict):
        content = content.get("text") or content.get("conversation") or content.get("body")
    body = str(data.get("text") or data.get("body") or content or f"[{data.get('messageType', 'evento')}]")
    try:
        message = _inbound(db, number, identity, phone, body, str(data.get("messageType", "text")))
    except ValueError:
        write_log(db, number.workspace_id, "warning", "webhook", "uazapi.message_without_phone", "Evento de mensagem UAZAPI ignorado por não conter telefone válido.", provider="uazapi", details={"event_type": event_type, "identity": identity})
        return
    write_log(db, number.workspace_id, "success", "webhook", "uazapi.message_received", f"Mensagem recebida via UAZAPI de {phone}", provider="uazapi", message_id=message.id)


def process_baileys_payload(db: Session, number: WhatsAppNumber, payload: dict) -> None:
    event_type = str(payload.get("event") or "event")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    identity = str(data.get("id") or f"{event_type}:{_payload_hash(payload)}")
    external_id = f"{number.id}:{event_type}:{identity}"
    if not _store_event(db, number.workspace_id, "baileys", external_id, event_type, payload):
        return
    if event_type == "connection":
        state = str(data.get("status") or "disconnected").lower()
        number.status = "connected" if data.get("connected") else state
        if data.get("phone"):
            try:
                number.phone_e164 = normalize_phone(str(data["phone"]))
            except ValueError:
                pass
        write_log(db, number.workspace_id, "info", "connection", "baileys.connection", f"Evento de conexão Baileys recebido: {state}", provider="baileys")
        return
    if event_type == "message":
        phone = str(data.get("from") or "").split("@", 1)[0]
        try:
            message = _inbound(db, number, identity, phone, str(data.get("text") or f"[{data.get('type', 'mensagem')}]"), str(data.get("type") or "text"))
        except ValueError:
            write_log(db, number.workspace_id, "warning", "webhook", "baileys.message_without_phone", "Evento Baileys ignorado por não conter telefone válido.", provider="baileys")
            return
        write_log(db, number.workspace_id, "success", "webhook", "baileys.message_received", f"Mensagem recebida via Baileys de {phone}", provider="baileys", message_id=message.id)
        return
    if event_type == "message_update":
        state = str(data.get("status") or "").lower()
        message = _update_status(db, identity, state)
        write_log(db, number.workspace_id, "error" if state == "failed" else "success", "delivery", "baileys.message_status", f"Status Baileys atualizado para {state}", provider="baileys", message_id=message.id if message else None)
        return
    write_log(db, number.workspace_id, "info", "webhook", "baileys.event_ignored", f"Evento Baileys sem processador específico: {event_type}", provider="baileys")
