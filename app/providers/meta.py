
import httpx

from ..config import settings
from . import ProviderError


class MetaProvider:
    def __init__(self, access_token: str):
        self.access_token = access_token or settings.whatsapp_access_token
        self.base_url = f"https://graph.facebook.com/{settings.meta_graph_version}"
        if not self.access_token:
            raise ProviderError("Informe um access token da Meta.", "missing_credentials")

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    async def verify_number(self, phone_number_id: str) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base_url}/{phone_number_id}",
                params={"fields": "display_phone_number,verified_name,quality_rating"},
                headers=self.headers,
            )
        return self._decode(response)

    async def send_text(self, phone_number_id: str, to: str, body: str) -> str:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url}/{phone_number_id}/messages", json=payload, headers=self.headers)
        data = self._decode(response)
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("A Meta aceitou a requisição sem retornar um message ID.") from exc

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("Resposta inválida recebida da Meta.", retryable=response.status_code >= 500) from exc
        if not response.is_success:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise ProviderError(error.get("message", "A Meta recusou a operação."), str(error.get("code", response.status_code)), response.status_code >= 500 or response.status_code == 429)
        return data
