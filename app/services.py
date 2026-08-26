import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Campaign, CampaignRecipient, CampaignStep, Consent, Contact, ListContact, Message, OutboxJob, WhatsAppNumber


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if 10 <= len(digits) <= 15:
        if len(digits) in (10, 11):
            digits = "55" + digits
        return "+" + digits
    raise ValueError("Informe um telefone válido com DDI e DDD.")


def render_variables(template: str, contact: Contact) -> str:
    values = {
        "nome": contact.name or "",
        "empresa": contact.company or "",
        "telefone": contact.phone_e164 or "",
        "email": contact.email or "",
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


@dataclass
class Simulation:
    total: int = 0
    eligible: int = 0
    opted_out: int = 0
    suppressed: int = 0
    frequency_capped: int = 0
    invalid: int = 0

    @property
    def selected(self) -> int:
        return self.total


def campaign_contacts(db: Session, workspace_id: str, campaign: Campaign) -> List[Contact]:
    return list(
        db.scalars(
            select(Contact)
            .join(ListContact, ListContact.contact_id == Contact.id)
            .where(ListContact.list_id == campaign.list_id, Contact.workspace_id == workspace_id)
            .order_by(Contact.created_at)
        )
    )


def simulate_campaign(db: Session, workspace_id: str, campaign: Campaign) -> Simulation:
    contacts = campaign_contacts(db, workspace_id, campaign)
    result = Simulation(total=len(contacts))
    since = datetime.now(timezone.utc) - timedelta(days=7)
    for contact in contacts:
        if contact.suppressed:
            result.suppressed += 1
            continue
        consent = db.scalar(select(Consent).where(Consent.contact_id == contact.id, Consent.channel == "whatsapp"))
        if not consent or consent.status != "opted_in":
            result.opted_out += 1
            continue
        if not contact.phone_e164:
            result.invalid += 1
            continue
        recent = db.scalar(
            select(func.count(Message.id)).where(
                Message.contact_id == contact.id,
                Message.direction == "outbound",
                Message.status.in_(["sent", "delivered", "read"]),
                Message.created_at >= since,
            )
        ) or 0
        if recent >= settings.max_messages_per_contact_7d:
            result.frequency_capped += 1
            continue
        result.eligible += 1
    return result


def enqueue_campaign(db: Session, workspace_id: str, campaign: Campaign) -> int:
    step = db.scalar(select(CampaignStep).where(CampaignStep.campaign_id == campaign.id).order_by(CampaignStep.position))
    number = db.get(WhatsAppNumber, campaign.phone_number_id)
    if not step or not number or number.workspace_id != workspace_id:
        raise ValueError("Campanha sem mensagem ou canal válido.")
    created = 0
    queued_from = datetime.now(timezone.utc)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    for contact in campaign_contacts(db, workspace_id, campaign):
        consent = db.scalar(select(Consent).where(Consent.contact_id == contact.id, Consent.channel == "whatsapp"))
        recent = db.scalar(select(func.count(Message.id)).where(Message.contact_id == contact.id, Message.created_at >= since, Message.status.in_(["sent", "delivered", "read"]))) or 0
        if contact.suppressed or not consent or consent.status != "opted_in" or recent >= settings.max_messages_per_contact_7d:
            continue
        key = f"campaign:{campaign.id}:contact:{contact.id}:step:1"
        if db.scalar(select(Message.id).where(Message.workspace_id == workspace_id, Message.idempotency_key == key)):
            continue
        message = Message(
            workspace_id=workspace_id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            phone_number_id=number.id,
            body=render_variables(step.body, contact),
            status="queued",
            idempotency_key=key,
        )
        db.add(message)
        db.flush()
        db.add(CampaignRecipient(workspace_id=workspace_id, campaign_id=campaign.id, contact_id=contact.id, status="queued"))
        delay_seconds = created * (60.0 / max(1, campaign.processing_rate))
        db.add(OutboxJob(workspace_id=workspace_id, message_id=message.id, idempotency_key=key, status="pending", available_at=queued_from + timedelta(seconds=delay_seconds)))
        created += 1
    campaign.status = "running"
    campaign.started_at = datetime.now(timezone.utc)
    return created
