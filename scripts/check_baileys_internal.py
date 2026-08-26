import argparse
import asyncio
import json

from app.config import settings
from app.providers.baileys import VercelBaileysProvider


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica uma sessão no Baileys interno.")
    parser.add_argument("session_id")
    args = parser.parse_args()
    provider = VercelBaileysProvider(settings.public_base_url, settings.app_secret, args.session_id)
    status = await provider.status()
    print(json.dumps({"connected": bool(status.get("connected")), "status": status.get("status")}))


if __name__ == "__main__":
    asyncio.run(main())
