import os
from pathlib import Path
from uuid import uuid4

import pytest

TEST_DB = Path("/private/tmp") / f"nexoflow-test-{uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["APP_SECRET"] = "test-secret-with-more-than-thirty-two-characters"
os.environ["RUN_WORKER"] = "0"
os.environ["SECURE_COOKIES"] = "0"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "verify-test"
os.environ["WHATSAPP_APP_SECRET"] = "meta-app-secret-test"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
