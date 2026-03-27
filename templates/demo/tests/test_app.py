"""Basic tests for the demo Flask app."""

import pytest
from app import app as flask_app


@pytest.fixture
def client():
    """Return a test client for the Flask app."""
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_hello_returns_200(client):
    """GET / should return HTTP 200."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_hello_json_structure(client):
    """GET / should return JSON with status field."""
    resp = client.get("/")
    data = resp.get_json()
    assert data is not None
    assert data["status"] == "ok"
    assert "message" in data
