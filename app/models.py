from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .db import Base


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = __import__("sqlalchemy").Column(DateTime(timezone=True), default=now, nullable=False)
    updated_at = __import__("sqlalchemy").Column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)


Column = __import__("sqlalchemy").Column


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id = Column(String(48), primary_key=True, default=lambda: uid("usr"))
    email = Column(String(320), nullable=False, unique=True, index=True)
    name = Column(String(160), nullable=False)
    password_hash = Column(String(512), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    id = Column(String(48), primary_key=True, default=lambda: uid("wsp"))
    name = Column(String(160), nullable=False)


class Membership(Base, TimestampMixin):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_user"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("mem"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(48), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="owner")
    workspace = relationship("Workspace")
    user = relationship("User")


class WhatsAppNumber(Base, TimestampMixin):
    __tablename__ = "whatsapp_phone_numbers"
    __table_args__ = (UniqueConstraint("workspace_id", "provider", "phone_number_id", name="uq_workspace_phone_id"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("num"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(32), nullable=False, default="meta_cloud")
    phone_number_id = Column(String(160), nullable=False)
    waba_id = Column(String(160))
    display_name = Column(String(160), nullable=False)
    phone_e164 = Column(String(32))
    status = Column(String(32), nullable=False, default="pending")
    quality_rating = Column(String(32))
    access_token_encrypted = Column(Text)
    webhook_secret = Column(String(160))

    @property
    def provider_label(self) -> str:
        return {"meta_cloud": "Meta oficial", "uazapi": "UAZAPI", "baileys": "Baileys"}.get(self.provider, self.provider)


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "phone_e164", name="uq_workspace_contact_phone"),
        Index("idx_contacts_workspace_created", "workspace_id", "created_at"),
    )
    id = Column(String(48), primary_key=True, default=lambda: uid("con"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(160), nullable=False)
    phone_e164 = Column(String(32), nullable=False)
    email = Column(String(320))
    company = Column(String(160))
    source = Column(String(80), nullable=False, default="Manual")
    suppressed = Column(Boolean, nullable=False, default=False)
    consent = relationship("Consent", uselist=False, back_populates="contact", cascade="all, delete-orphan")


class Consent(Base, TimestampMixin):
    __tablename__ = "contact_consents"
    __table_args__ = (UniqueConstraint("contact_id", "channel", name="uq_contact_channel_consent"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("cns"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(String(48), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(32), nullable=False, default="whatsapp")
    status = Column(String(32), nullable=False, default="unknown")
    source = Column(String(160))
    granted_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    contact = relationship("Contact", back_populates="consent")


class ContactList(Base, TimestampMixin):
    __tablename__ = "contact_lists"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_workspace_list_name"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("lst"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(160), nullable=False)
    description = Column(Text)


class ListContact(Base):
    __tablename__ = "list_contacts"
    __table_args__ = (UniqueConstraint("list_id", "contact_id", name="uq_list_contact"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("lc"))
    list_id = Column(String(48), ForeignKey("contact_lists.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(String(48), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"
    __table_args__ = (Index("idx_campaign_status", "workspace_id", "status"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("cmp"))
    workspace_id = Column(String(48), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    list_id = Column(String(48), ForeignKey("contact_lists.id"), nullable=False)
    phone_number_id = Column(String(48), ForeignKey("whatsapp_phone_numbers.id"), nullable=False)
    name = Column(String(160), nullable=False)
    status = Column(String(32), nullable=False, default="draft")
    processing_rate = Column(Integer, nullable=False, default=20)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class CampaignStep(Base, TimestampMixin):
    __tablename__ = "campaign_steps"
    __table_args__ = (UniqueConstraint("campaign_id", "position", name="uq_campaign_step_position"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("stp"))
    campaign_id = Column(String(48), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False, default=1)
    body = Column(Text, nullable=False)


class CampaignRecipient(Base, TimestampMixin):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_recipient"),
        Index("idx_recipient_status", "campaign_id", "status"),
    )
    id = Column(String(48), primary_key=True, default=lambda: uid("rcp"))
    workspace_id = Column(String(48), nullable=False, index=True)
    campaign_id = Column(String(48), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(String(48), ForeignKey("contacts.id"), nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    reason = Column(String(160))


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_workspace_message_key"),
        Index("idx_message_campaign_status", "campaign_id", "status"),
    )
    id = Column(String(48), primary_key=True, default=lambda: uid("msg"))
    workspace_id = Column(String(48), nullable=False, index=True)
    campaign_id = Column(String(48), ForeignKey("campaigns.id"))
    contact_id = Column(String(48), ForeignKey("contacts.id"), nullable=False)
    phone_number_id = Column(String(48), ForeignKey("whatsapp_phone_numbers.id"), nullable=False)
    direction = Column(String(16), nullable=False, default="outbound")
    type = Column(String(24), nullable=False, default="text")
    body = Column(Text)
    status = Column(String(32), nullable=False, default="queued")
    provider_message_id = Column(String(200), index=True)
    error_message = Column(Text)
    idempotency_key = Column(String(160), nullable=False)
    sent_at = Column(DateTime(timezone=True))


class OutboxJob(Base, TimestampMixin):
    __tablename__ = "outbox_jobs"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_workspace_job_key"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("job"))
    workspace_id = Column(String(48), nullable=False, index=True)
    message_id = Column(String(48), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    idempotency_key = Column(String(160), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, default=now)
    locked_at = Column(DateTime(timezone=True))
    last_error = Column(Text)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("workspace_id", "contact_id", "phone_number_id", name="uq_workspace_conversation"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("cv"))
    workspace_id = Column(String(48), nullable=False, index=True)
    contact_id = Column(String(48), ForeignKey("contacts.id"), nullable=False)
    phone_number_id = Column(String(48), ForeignKey("whatsapp_phone_numbers.id"), nullable=False)
    status = Column(String(32), nullable=False, default="open")
    last_message_at = Column(DateTime(timezone=True), nullable=False, default=now)


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_messages"
    id = Column(String(48), primary_key=True, default=lambda: uid("cvm"))
    conversation_id = Column(String(48), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(String(48), ForeignKey("messages.id"))
    direction = Column(String(16), nullable=False)
    type = Column(String(24), nullable=False, default="text")
    body = Column(Text)


class WebhookEvent(Base, TimestampMixin):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_provider_event"),)
    id = Column(String(48), primary_key=True, default=lambda: uid("evt"))
    workspace_id = Column(String(48), index=True)
    provider = Column(String(32), nullable=False)
    external_id = Column(String(200), nullable=False)
    event_type = Column(String(100), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text)
    processed_at = Column(DateTime(timezone=True))


class SystemLog(Base):
    __tablename__ = "system_logs"
    __table_args__ = (
        Index("idx_system_log_workspace_created", "workspace_id", "created_at"),
        Index("idx_system_log_level_created", "level", "created_at"),
    )
    id = Column(String(48), primary_key=True, default=lambda: uid("log"))
    workspace_id = Column(String(48), index=True)
    level = Column(String(16), nullable=False)
    category = Column(String(32), nullable=False)
    event = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    provider = Column(String(32))
    campaign_id = Column(String(48))
    contact_id = Column(String(48))
    message_id = Column(String(48))
    details_json = Column(Text, default="{}")
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)


class RiskRule(Base, TimestampMixin):
    __tablename__ = "risk_rules"
    id = Column(String(48), primary_key=True, default=lambda: uid("rsk"))
    workspace_id = Column(String(48), unique=True, nullable=False)
    failure_rate_threshold = Column(Float, nullable=False, default=0.08)
    opt_out_rate_threshold = Column(Float, nullable=False, default=0.02)
    pause_on_auth_error = Column(Boolean, nullable=False, default=True)
