import argparse

from app.db import SessionLocal
from app.models import WhatsAppNumber


def main() -> None:
    parser = argparse.ArgumentParser(description="Move um canal Baileys para a função interna da Vercel.")
    parser.add_argument("number_id")
    args = parser.parse_args()

    with SessionLocal() as db:
        number = db.get(WhatsAppNumber, args.number_id)
        if number is None:
            raise SystemExit("Canal não encontrado.")
        if number.provider != "baileys":
            raise SystemExit("O canal informado não é Baileys.")
        number.waba_id = "vercel-internal"
        number.status = "disconnected"
        db.commit()
        print(f"Canal migrado: {number.id} ({number.display_name})")


if __name__ == "__main__":
    main()
