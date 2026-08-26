import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from .config import settings
from .db import session_scope
from .log_service import write_log
from .models import Campaign, CampaignRecipient, Contact, Message, OutboxJob, WhatsAppNumber
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
        try:
            token = decrypt_secret(number.access_token_encrypted or "")
            if number.provider == "meta_cloud":
                provider_id = await MetaProvider(token).send_text(number.phone_number_id, contact.phone_e164, message.body or "")
            elif number.provider == "uazapi":
                provider = UazapiProvider(instance_token=token)
                if not await provider.check_number(contact.phone_e164):
                    raise ProviderError("O número do destinatário não está cadastrado no WhatsApp.", "recipient_not_on_whatsapp", False)
                provider_id = await provider.send_text(contact.phone_e164, message.body or "")
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


def _next_job(workspace_id: Optional[str] = None) -> str:
    with session_scope() as db:
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
                campaign.status = "failed" if failed else "completed"
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
                else:
                    write_log(db, campaign.workspace_id, "success", "campaign", "campaign.completed", "Campanha concluída.", campaign_id=campaign.id)


async def process_available_jobs(workspace_id: str, max_jobs: int = 1) -> int:
    """Processa um lote curto, adequado a uma requisição serverless autenticada."""
    processed = 0
    for _ in range(max(1, min(max_jobs, 10))):
        job_id = await asyncio.to_thread(_next_job, workspace_id)
        if not job_id:
            break
        await _dispatch(job_id)
        processed += 1
    await asyncio.to_thread(_complete_campaigns)
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
