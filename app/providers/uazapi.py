import base64
import io
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import qrcode

from ..config import settings
from . import ProviderError


@dataclass(frozen=True)
class UazapiConnection:
    status: str
    connected: bool
    logged_in: bool
    qrcode: Optional[str] = None
    paircode: Optional[str] = None
    phone: Optional[str] = None


class UazapiProvider:
    def __init__(self, instance_token: str = "", admin_token: str = ""):
        if not settings.uazapi_server_url.startswith("https://"):
            raise ProviderError("A URL da UAZAPI precisa usar HTTPS.", "invalid_base_url")
        self.base_url = settings.uazapi_server_url
        self.instance_token = instance_token
        self.admin_token = admin_token

    @property
    def headers(self):
        token = self.admin_token or self.instance_token
        if not token:
            raise ProviderError("Informe o token da UAZAPI.", "missing_credentials")
        label = "admintoken" if self.admin_token else "token"
        return {label: token, "Content-Type": "application/json"}

    async def create_instance(self, name: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/instance/create", json={"name": name}, headers=self.headers)
        return self._decode(response)

    async def connect(self, phone: Optional[str] = None) -> dict:
        payload = {"phone": phone.lstrip("+")} if phone else {"browser": "auto", "systemName": "Nexo Flow"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/instance/connect", json=payload, headers=self.headers)
        return self._decode(response)

    async def status(self) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.base_url}/instance/status", headers=self.headers)
        return self._decode(response)

    async def check_number(self, number: str) -> bool:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/chat/check",
                json={"numbers": [number.lstrip("+")]},
                headers=self.headers,
            )
        data = self._decode(response)
        results = data.get("data") or data.get("results") or data.get("response") or []
        if isinstance(results, dict):
            results = results.get("data") or results.get("results") or [results]
        if not isinstance(results, list) or not results:
            raise ProviderError("A UAZAPI não conseguiu validar o destinatário.", "recipient_check_failed", True)
        result = results[0] if isinstance(results[0], dict) else {}
        return bool(result.get("isInWhatsapp") or result.get("isInWhatsApp") or result.get("exists"))

    async def send_text(self, to: str, body: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/send/text", json={"number": to.lstrip("+"), "text": body}, headers=self.headers)
        data = self._decode(response)
        containers = [data]
        containers.extend(item for item in (data.get("data"), data.get("message"), data.get("response")) if isinstance(item, dict))
        message_id = next((item.get("messageid") or item.get("messageId") or item.get("id") for item in containers if item.get("messageid") or item.get("messageId") or item.get("id")), None)
        if not message_id:
            raise ProviderError("A UAZAPI aceitou a requisição sem retornar o ID da mensagem.")
        return str(message_id)

    @staticmethod
    def connection(data: dict) -> UazapiConnection:
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        instance = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
        status_data = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        instance_status = instance.get("status")
        raw_status = instance_status if isinstance(instance_status, str) else payload.get("state")
        if not isinstance(raw_status, str) and isinstance(payload.get("status"), str):
            raw_status = payload.get("status")
        connected = bool(status_data.get("connected") or payload.get("connected"))
        logged_in = bool(status_data.get("loggedIn") or payload.get("loggedIn"))
        status = str(raw_status or ("connected" if connected and logged_in else "connecting" if connected else "disconnected")).lower()
        if connected and logged_in:
            status = "connected"
        jid = status_data.get("jid") or payload.get("jid")
        phone = str(jid.get("user")) if isinstance(jid, dict) and jid.get("user") else None
        if not phone and isinstance(jid, str):
            phone = jid.split("@", 1)[0]
        qrcode_value = (
            instance.get("qrcode")
            or status_data.get("qrcode")
            or payload.get("qrcode")
            or payload.get("qr")
            or data.get("qrcode")
            or data.get("qr")
        )
        return UazapiConnection(
            status=status,
            connected=connected or status == "connected",
            logged_in=logged_in or status == "connected",
            qrcode=UazapiProvider._qr_data_url(qrcode_value),
            paircode=(
                instance.get("paircode")
                or status_data.get("paircode")
                or payload.get("paircode")
                or payload.get("pairCode")
                or data.get("paircode")
                or data.get("pairCode")
            ),
            phone=phone,
        )

    @staticmethod
    def _qr_data_url(value: Any) -> Optional[str]:
        """Normalize the QR variants returned by different UAZAPI releases."""
        if isinstance(value, dict):
            for key in ("base64", "image", "qrcode", "qr", "data", "code"):
                if value.get(key):
                    return UazapiProvider._qr_data_url(value[key])
            return None
        if not isinstance(value, str) or not value.strip():
            return None

        raw_value = value.strip()
        if raw_value.startswith("data:image/") or raw_value.startswith(("https://", "http://")):
            return raw_value

        compact = "".join(raw_value.split())
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
        except (ValueError, base64.binascii.Error):
            decoded = b""
        media_type = None
        if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif decoded.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif decoded.startswith((b"GIF87a", b"GIF89a")):
            media_type = "image/gif"
        elif decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP":
            media_type = "image/webp"
        if media_type:
            return f"data:{media_type};base64,{compact}"

        image = qrcode.make(raw_value)
        output = io.BytesIO()
        image.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    async def configure_webhook(self, url: str) -> dict:
        payload = {"url": url, "enabled": True, "events": ["messages", "messages_update", "connection"], "excludeMessages": ["wasSentByApi"]}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/webhook", json=payload, headers=self.headers)
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("Resposta inválida recebida da UAZAPI.", retryable=response.status_code >= 500) from exc
        if not response.is_success:
            message = (
                data.get("message_ptbr")
                or data.get("provider_message_ptbr")
                or data.get("error")
                or data.get("message")
                or "A UAZAPI recusou a operação."
            )
            normalized = str(message).lower()
            if "not on whatsapp" in normalized or "não está no whatsapp" in normalized or "nao esta no whatsapp" in normalized:
                raise ProviderError("O número do destinatário não está cadastrado no WhatsApp.", "recipient_not_on_whatsapp", False)
            retryable = response.status_code >= 500 or response.status_code == 429
            raise ProviderError(str(message), str(response.status_code), retryable)
        return data if isinstance(data, dict) else {"data": data}
