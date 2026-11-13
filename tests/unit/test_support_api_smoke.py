from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from packages.model_gateway.configured_route import Endpoint
from scripts import support_api_smoke as smoke


@pytest.mark.parametrize("disable_status", [200, 503])
def test_profile_setup_failure_preserves_error_and_independent_cleanup(monkeypatch, disable_status):
    route = Endpoint(
        name="aliyun-rag",
        provider="openai-compatible",
        model=smoke.MODEL,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="synthetic-profile-setup-key",
    )
    credential_id, workspace_id = str(uuid4()), str(uuid4())
    base = "/api/v1/workspaces/" + workspace_id
    customer = None
    calls = []

    def api(req: httpx.Request):
        nonlocal customer
        path = req.url.path
        data = json.loads(req.content) if req.content else None
        calls.append((req.method, path, data))
        if path == "/api/v1/auth/register":
            return httpx.Response(201, json={"user_id": str(uuid4()), "access_token": "synthetic"})
        if path == "/api/v1/organizations":
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/api/v1/workspaces":
            return httpx.Response(201, json={"id": workspace_id})
        if path.endswith("/members"):
            return httpx.Response(201, json={})
        if path == base + "/support/customers":
            if req.method == "GET":
                items = [customer] if customer else []
                return httpx.Response(200, json={"items": items, "total": len(items)})
            if customer is None:
                customer = {"id": str(uuid4()), **data}
                return httpx.Response(201, json=customer)
            if customer["name"] == data["name"]:
                return httpx.Response(200, json=customer)
            return httpx.Response(409, json={"error": {"code": "CUSTOMER_REF_CONFLICT"}})
        if path == base + "/provider-credentials":
            return httpx.Response(201, json={"id": credential_id})
        if path == base + "/model-profiles":
            return httpx.Response(422, json={"error": {"code": "VALIDATION_ERROR"}})
        credential = base + "/provider-credentials/" + credential_id
        assert path in {credential, credential + "/rotate-secret"}
        if req.method == "PATCH":
            return httpx.Response(disable_status, json={})
        return httpx.Response(200, json={"id": credential_id})

    client = httpx.Client
    monkeypatch.setattr(smoke, "aliyun_route", lambda: route)
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **options: client(transport=httpx.MockTransport(api), **options),
    )
    report = {"passed": False, "runs": []}
    with pytest.raises(smoke.SmokeFailure, match="model-profiles returned HTTP 422"):
        smoke.run("http://127.0.0.1:8080/api/v1", report)
    assert report == {
        "passed": False,
        "runs": [],
        "workspace_id": workspace_id,
        "credential_cleanup": {
            "credential_id": credential_id,
            "disable": {
                "confirmed": disable_status == 200,
                "failures": [] if disable_status == 200 else ["HTTP 503"] * 3,
            },
            "rotate": {"confirmed": True, "failures": []},
        },
    }
    credential = base + "/provider-credentials/" + credential_id
    assert calls[-2] == ("PATCH", credential, {"enabled": False})
    method, path, body = calls[-1]
    assert (method, path) == ("POST", credential + "/rotate-secret")
    assert body["secret"] and body["secret"] != route.api_key


def test_cleanup_renews_owner_on_401_and_retries_rotate_with_the_same_value():
    owner = {"email": "synthetic@example.test", "password": "private-password"}
    writes, rotations = [], []
    credential_id = str(uuid4())

    def api(req):
        path, data = req.url.path, json.loads(req.content)
        if path == "/auth/login":
            assert data == owner
            return httpx.Response(200, json={"access_token": "refreshed-token"})
        writes.append(req.method)
        if req.method == "PATCH":
            assert path == "/provider-credentials/" + credential_id and data == {"enabled": False}
            if req.headers.get("Authorization") != "Bearer refreshed-token":
                return httpx.Response(401, json={"error": {"message": "private-response"}})
        else:
            assert path == f"/provider-credentials/{credential_id}/rotate-secret"
            rotations.append(data["secret"])
            if len(rotations) == 1:
                return httpx.Response(503, json={"error": {"message": "private-response"}})
        return httpx.Response(200, json={})

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(api)) as client:
        result = smoke.retire_credential(
            client, owner, "provider-credentials", credential_id, "unused"
        )
    assert writes == ["PATCH", "PATCH", "POST", "POST"]
    assert len(rotations) == 2 and rotations[0] == rotations[1]
    assert result == {
        "credential_id": credential_id,
        "disable": {"confirmed": True, "failures": ["HTTP 401"]},
        "rotate": {"confirmed": True, "failures": ["HTTP 503"]},
    }


def test_cleanup_recovers_an_unacknowledged_create_and_rotates_despite_disabled_route_failure():
    target, other = str(uuid4()), str(uuid4())
    writes = []

    def api(req):
        if req.method == "GET":
            return httpx.Response(
                200, json=[{"id": other, "name": "other"}, {"id": target, "name": "new-job"}]
            )
        writes.append((req.method, req.url.path))
        if req.method == "PATCH":
            return httpx.Response(503, json={"error": {"message": "private-response"}})
        return httpx.Response(200)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(api)) as client:
        result = smoke.retire_credential(client, {}, "provider-credentials", None, "new-job")
    expected_path = "/provider-credentials/" + target
    assert writes == [("PATCH", expected_path)] * 3 + [("POST", expected_path + "/rotate-secret")]
    assert result == {
        "lookup": {"confirmed": True, "failures": []},
        "credential_id": target,
        "disable": {"confirmed": False, "failures": ["HTTP 503"] * 3},
        "rotate": {"confirmed": True, "failures": []},
    }
