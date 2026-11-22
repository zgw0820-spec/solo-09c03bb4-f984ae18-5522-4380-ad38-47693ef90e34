"""HTTP-level tests against the FastAPI app using its live endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.defaults import DEFAULT_LAYOUT
import app.main as main_module


@pytest.fixture()
def client():
    main_module.sim = main_module.Simulator(DEFAULT_LAYOUT)
    main_module.service = main_module.BatchService(main_module.sim)
    with TestClient(main_module.app) as c:
        yield c


def plan_body(spec_a="1g.10gb", spec_b="1g.10gb"):
    return {"new_slices": [{"spec": spec_a}, {"spec": spec_b}]}


def test_state_and_index(client):
    assert client.get("/").status_code == 200
    data = client.get("/api/state").json()
    assert data["layout"]["slot_count"] == 8
    assert len(data["layout"]["slices"]) >= 1
    assert data["batch"] is None


def test_requires_exactly_two_slices(client):
    res = client.post("/api/plan", json={"new_slices": [{"spec": "1g.10gb"}]})
    assert res.status_code == 422
    res = client.post(
        "/api/plan",
        json={"new_slices": [{"spec": "1g.10gb"}] * 3},
    )
    assert res.status_code == 422


def test_full_plan_confirm_flow(client):
    data = client.post("/api/plan", json=plan_body()).json()
    assert data["batch"]["plan"]["ok"] is True
    assert data["batch"]["state"] == "planned"
    data = client.post("/api/batch/confirm").json()
    assert data["batch"]["state"] == "delivered"
    assert data["batch"]["actual"]["layout_matches_plan"] is True


def test_confirm_conflict_returns_planned(client):
    data = client.post("/api/plan", json=plan_body()).json()
    removal = data["batch"]["plan"]["removals"][0]["slice_id"]
    # External order arrives before confirmation.
    res = client.post("/api/order", json={"slice_id": removal})
    assert res.status_code == 200
    data = client.post("/api/batch/confirm").json()
    assert data["batch"]["state"] == "planned"
    assert data["batch"]["error"]


def test_driver_error_then_retry_via_http(client):
    client.post("/api/plan", json=plan_body())
    client.post("/api/fault", json={"mode": "before_apply", "skip": 0, "count": 1})
    data = client.post("/api/batch/confirm").json()
    assert data["batch"]["state"] == "failed"
    data = client.post("/api/batch/retry").json()
    assert data["batch"]["state"] == "delivered"


def test_selftest_endpoint_all_pass(client):
    data = client.post("/api/selftest").json()
    assert data["passed"] is True
    assert data["summary"]["total"] == data["summary"]["passed"]


def test_running_slice_order_rejected(client):
    res = client.post("/api/order", json={"slice_id": "train-a"})
    assert res.status_code == 409


def test_layout_import_validation_errors_are_422(client):
    res = client.post(
        "/api/layout/import",
        json={
            "slot_count": 4,
            "specs": [{"name": "s", "allowed_intervals": [{"start": 0, "end": 1}]}],
            "slices": [
                {"id": "a", "spec": "s", "interval": {"start": 0, "end": 1}},
                {"id": "b", "spec": "s", "interval": {"start": 0, "end": 1}},
            ],
        },
    )
    assert res.status_code == 422
    assert "重叠" in res.text
