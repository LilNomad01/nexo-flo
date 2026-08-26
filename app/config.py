from functools import lru_cache
from os import getenv


def _bool(name: str, default: bool) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.is_vercel = bool(getenv("VERCEL"))
        self.app_env = getenv("APP_ENV", "development")
        self.app_secret = getenv("APP_SECRET", "change-this-to-a-long-random-value")
        self.database_url = getenv("DATABASE_URL", "sqlite:///./data/nexoflow.db")
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        elif self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        vercel_host = getenv("VERCEL_PROJECT_PRODUCTION_URL") or getenv("VERCEL_URL")
        default_public_url = f"https://{vercel_host}" if vercel_host else "http://localhost:8000"
        self.public_base_url = getenv("PUBLIC_BASE_URL", default_public_url).rstrip("/")
        self.meta_graph_version = getenv("META_GRAPH_VERSION", "v23.0")
        self.whatsapp_access_token = getenv("WHATSAPP_ACCESS_TOKEN", "")
        self.whatsapp_verify_token = getenv("WHATSAPP_VERIFY_TOKEN", "")
        self.whatsapp_app_secret = getenv("WHATSAPP_APP_SECRET", "")
        self.uazapi_server_url = getenv("UAZAPI_SERVER_URL", "https://free.uazapi.com").rstrip("/")
        self.max_messages_per_minute = _int("MAX_MESSAGES_PER_MINUTE", 40)
        self.max_messages_per_contact_7d = _int("MAX_MESSAGES_PER_CONTACT_7D", 2)
        self.failure_rate_pause_threshold = _float("FAILURE_RATE_PAUSE_THRESHOLD", 0.08)
        self.opt_out_rate_pause_threshold = _float("OPT_OUT_RATE_PAUSE_THRESHOLD", 0.02)
        self.allow_registration = _bool("ALLOW_REGISTRATION", not self.is_production)
        self.run_worker = _bool("RUN_WORKER", not self.is_vercel)
        self.secure_cookies = _bool("SECURE_COOKIES", self.app_env == "production")
        self.session_max_age = _int("SESSION_MAX_AGE", 60 * 60 * 24 * 14)
        self.max_import_rows = _int("MAX_IMPORT_ROWS", 5000)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
