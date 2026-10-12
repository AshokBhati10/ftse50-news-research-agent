"""FTSE API proxy to research-stack's job API (mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from api.app import create_app

STACK = "http://127.0.0.1:8001"
JOB = "a" * 32


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_submit_proxies_to_job_api_and_returns_job_id(client):
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{STACK}/research/jobs").mock(
            return_value=httpx.Response(202, json={"job_id": JOB, "status": "pending"}))
        r = client.post("/api/deep-research", json={"question": "What is X?"})
    assert r.status_code == 202 and r.json() == {"job_id": JOB, "status": "pending"}
    assert route.calls[0].request.content == b'{"question":"What is X?","fresh":false}'


def test_history_list_is_proxied(client):
    rows = [{"job_id": JOB, "question": "q", "status": "completed", "created_at": "2026-10-12T10:00:00Z"}]
    with respx.mock() as router:
        router.get(f"{STACK}/research/jobs").mock(return_value=httpx.Response(200, json=rows))
        r = client.get("/api/deep-research")
    assert r.status_code == 200 and r.json() == rows


def test_status_is_proxied_including_result(client):
    with respx.mock() as router:
        router.get(f"{STACK}/research/jobs/{JOB}").mock(return_value=httpx.Response(
            200, json={"job_id": JOB, "status": "completed", "result": {"answer_markdown": "A"}}))
        r = client.get(f"/api/deep-research/{JOB}")
    assert r.json()["status"] == "completed" and r.json()["result"]["answer_markdown"] == "A"


def test_upstream_errors_keep_their_status_and_detail(client):
    with respx.mock() as router:
        router.get(f"{STACK}/research/jobs/{JOB}").mock(
            return_value=httpx.Response(404, json={"detail": "unknown job id"}))
        router.post(f"{STACK}/research/jobs").mock(
            return_value=httpx.Response(503, json={"detail": "job queue unavailable"}))
        assert client.get(f"/api/deep-research/{JOB}").status_code == 404
        r = client.post("/api/deep-research", json={"question": "What is X?"})
    assert r.status_code == 503 and "queue unavailable" in r.json()["detail"]


def test_unreachable_stack_is_a_clear_502(client):
    with respx.mock() as router:
        router.post(f"{STACK}/research/jobs").mock(side_effect=httpx.ConnectError("refused"))
        r = client.post("/api/deep-research", json={"question": "What is X?"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_input_validation_and_job_id_shape(client):
    assert client.post("/api/deep-research", json={"question": "x"}).status_code == 422
    assert client.get("/api/deep-research/not-a-job-id").status_code == 404
    assert client.get("/api/deep-research/..%2Fadmin").status_code == 404
