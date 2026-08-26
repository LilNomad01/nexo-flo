import base64
import hashlib
import io
import hmac
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
import qrcode
import qrcode.image.svg

from . import ProviderError
from .uazapi import UazapiProvider


@dataclass(frozen=True)
class BaileysConnection:
    status: str
    connected: bool
    qrcode: Optional[str] = None
    qrcode_svg: Optional[str] = None
    phone: Optional[str] = None
    last_error: Optional[str] = None


class BaileysProvider:
    def __init__(self, base_url: str, api_token: str, session_id: str):
        self.base_url = self._base_url(base_url)
        self.api_token = api_token.strip()
        self.session_id = session_id.strip()
        if not self.api_token:
            raise ProviderError("Informe o token do gateway Baileys.", "missing_credentials")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{4,80}", self.session_id):
            raise ProviderError("O ID da sessão Baileys é inválido.", "invalid_session_id")

    @staticmethod
    def _base_url(value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("A URL do gateway Baileys precisa usar HTTPS.", "invalid_base_url")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
            raise ProviderError("A URL do gateway Baileys precisa ser pública.", "invalid_base_url")
        return value.strip().rstrip("/")

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}

    @property
    def session_url(self):
        return f"{self.base_url}/sessions/{self.session_id}"

    async def create_session(self, webhook_url: str, webhook_secret: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.session_url,
                    json={"webhookUrl": webhook_url, "webhookSecret": webhook_secret},
                    headers=self.headers,
                )
        except httpx.RequestError as exc:
            raise ProviderError("O gateway Baileys está indisponível.", "gateway_unavailable", True) from exc
        return self._decode(response)

    async def status(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{self.session_url}/status", headers=self.headers)
        except httpx.RequestError as exc:
            raise ProviderError("O gateway Baileys está indisponível.", "gateway_unavailable", True) from exc
        return self._decode(response)

    async def connect(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{self.session_url}/connect", json={}, headers=self.headers)
        except httpx.RequestError as exc:
            raise ProviderError("O gateway Baileys está indisponível.", "gateway_unavailable", True) from exc
        return self._decode(response)

    async def check_number(self, number: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.session_url}/check",
                    json={"numbers": [number.lstrip("+")]},
                    headers=self.headers,
                )
        except httpx.RequestError as exc:
            raise ProviderError("O gateway Baileys está indisponível.", "gateway_unavailable", True) from exc
        data = self._decode(response)
        results = data.get("results") if isinstance(data.get("results"), list) else []
        return bool(results and isinstance(results[0], dict) and results[0].get("exists"))

    async def send_text(self, to: str, body: str, request_id: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(
                    f"{self.session_url}/messages",
                    json={"to": to.lstrip("+"), "text": body, "requestId": request_id},
                    headers=self.headers,
                )
        except httpx.RequestError as exc:
            raise ProviderError("O gateway Baileys está indisponível.", "gateway_unavailable", True) from exc
        data = self._decode(response)
        message_id = data.get("id")
        if not message_id:
            raise ProviderError("O gateway Baileys aceitou o envio sem retornar o ID da mensagem.")
        return str(message_id)

    @staticmethod
    def connection(data: dict) -> BaileysConnection:
        status = str(data.get("status") or ("connected" if data.get("connected") else "disconnected")).lower()
        raw_qr = data.get("qrcode") or data.get("qr")
        return BaileysConnection(
            status=status,
            connected=bool(data.get("connected") or status == "connected"),
            qrcode=UazapiProvider._qr_data_url(raw_qr),
            qrcode_svg=BaileysProvider._qr_svg(raw_qr),
            phone=str(data.get("phone")) if data.get("phone") else None,
            last_error=BaileysProvider._display_error(data.get("lastError")),
        )

    @staticmethod
    def _qr_svg(value: object) -> Optional[str]:
        """Render a raw Baileys QR payload as inline SVG.

        Inline SVG is used as the primary browser renderer because it does not
        require a second image request and is not affected by data-URL image
        loading quirks. Existing image/base64/URL QR variants continue to use
        the PNG/image fallback handled by ``_qr_data_url``.
        """
        if isinstance(value, dict):
            for key in ("qrcode", "qr", "data", "code", "base64", "image"):
                if value.get(key):
                    return BaileysProvider._qr_svg(value[key])
            return None
        if not isinstance(value, str) or not value.strip():
            return None

        raw_value = value.strip()
        if raw_value.startswith("data:image/") or raw_value.startswith(("https://", "http://")):
            return None

        compact = "".join(raw_value.split())
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
        except (ValueError, base64.binascii.Error):
            decoded = b""
        if (
            decoded.startswith(b"\x89PNG\r\n\x1a\n")
            or decoded.startswith(b"\xff\xd8\xff")
            or decoded.startswith((b"GIF87a", b"GIF89a"))
            or (decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP")
        ):
            return None

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=3, box_size=8)
        qr.add_data(raw_value)
        qr.make(fit=True)
        image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        output = io.BytesIO()
        image.save(output)
        svg = output.getvalue().decode("utf-8")
        if svg.startswith("<?xml"):
            svg = svg.split("?>", 1)[1].lstrip()
        return svg

    @staticmethod
    def _display_error(value: object) -> Optional[str]:
        if not value:
            return None
        message = str(value).strip()
        normalized = message.lower()
        if "timeout" in normalized or "expirou" in normalized:
            return "O QR expirou. Gere uma nova conexão."
        if message.startswith(("Sessão ", "O QR ", "Não foi ")):
            return message
        return "A conexão foi encerrada. Gere uma nova conexão."

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("Resposta inválida recebida do gateway Baileys.", retryable=response.status_code >= 500) from exc
        if not response.is_success:
            message = data.get("error") or data.get("message") or "O gateway Baileys recusou a operação."
            normalized = str(message).lower()
            permanent = response.status_code in {400, 401, 403, 404, 422} or "não está cadastrado" in normalized
            raise ProviderError(str(message), str(response.status_code), not permanent)
        return data if isinstance(data, dict) else {"data": data}


class VercelBaileysProvider:
    def __init__(self, base_url: str, app_secret: str, session_id: str):
        self.base_url = base_url.rstrip("/")
        self.app_secret = app_secret
        self.session_id = session_id
        if not self.base_url.startswith("https://"):
            raise ProviderError("A URL pública do Nexo Flow precisa usar HTTPS.", "invalid_base_url")
        if len(self.app_secret) < 32:
            raise ProviderError("APP_SECRET inválido para o Baileys interno.", "invalid_app_secret")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{4,80}", self.session_id):
            raise ProviderError("O ID da sessão Baileys é inválido.", "invalid_session_id")

    async def _call(self, action: str, payload: Optional[dict] = None, timeout: float = 45) -> dict:
        body = payload or {}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.app_secret.encode(),
            f"{timestamp}.{action}.{self.session_id}.{canonical}".encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/baileys-internal",
                    params={"action": action, "sessionId": self.session_id},
                    content=canonical.encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Nexo-Timestamp": timestamp,
                        "X-Nexo-Signature": signature,
                    },
                )
        except httpx.RequestError as exc:
            raise ProviderError("O Baileys interno da Vercel está indisponível.", "gateway_unavailable", True) from exc
        return BaileysProvider._decode(response)

    async def create_session(self, webhook_url: str = "", webhook_secret: str = "") -> dict:
        return await self._call("create", timeout=35)

    async def status(self) -> dict:
        return await self._call("status", timeout=15)

    async def connect(self) -> dict:
        return await self._call("connect", timeout=35)

    async def check_number(self, number: str) -> bool:
        data = await self._call("check", {"numbers": [number.lstrip("+")]}, timeout=40)
        results = data.get("results") if isinstance(data.get("results"), list) else []
        return bool(results and isinstance(results[0], dict) and results[0].get("exists"))

    async def send_text(self, to: str, body: str, request_id: str) -> str:
        data = await self._call("messages", {"requestId": request_id, "text": body, "to": to.lstrip("+")}, timeout=55)
        message_id = data.get("id")
        if not message_id:
            raise ProviderError("O Baileys aceitou o envio sem retornar o ID da mensagem.")
        return str(message_id)

    connection = staticmethod(BaileysProvider.connection)
