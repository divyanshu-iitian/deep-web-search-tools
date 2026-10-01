from fastapi.testclient import TestClient

from deepsearch.api import app
from deepsearch.providers import _hits


def test_dashboard_and_fictional_search():
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
    payload = {"name": "Maya Chen", "company": "Cedar Utilities",
               "company_domain": "cedar.example.org", "demo": True}
    response = client.post("/api/search", json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "corroborated"
    assert client.get(f"/api/runs/{report['run_id']}").status_code == 200
    assert client.delete(f"/api/runs/{report['run_id']}").json() == {"deleted": True}
    assert client.get(f"/api/runs/{report['run_id']}").status_code == 404


def test_web_search_results_reject_non_https_and_deduplicate():
    hits = _hits("brave", "query", [
        {"title": "Safe", "url": "https://example.org/a", "description": "Public"},
        {"title": "Duplicate", "url": "https://example.org/a", "description": "Public"},
        {"title": "Unsafe", "url": "http://127.0.0.1/private", "description": "Private"},
    ], "description")
    assert len(hits) == 1
    assert hits[0].url == "https://example.org/a"
