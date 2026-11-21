"""Phase 2 tests: configuration and the health endpoint."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def test_settings_defaults():
    s = get_settings()
    assert s.mongo_db_name == "BharatMarketDB"
    assert "localhost" in s.mongo_uri.get_secret_value()
    assert s.cors_origin_list  # at least one allowed origin


def test_mongo_uri_is_hidden_in_repr():
    """The URI may contain a password, so it must never appear in logs."""
    assert "mongodb://" not in repr(get_settings().mongo_uri)


@pytest.mark.integration
def test_health_endpoint_reports_mongodb():
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"]["connected"] is True
    assert body["database"]["database"] == "BharatMarketDB"
