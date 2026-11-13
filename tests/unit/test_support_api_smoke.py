from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from packages.model_gateway.configured_route import Endpoint
from scripts import support_api_smoke as smoke


def test_profile_setup_failure_retires_only_the_new_provider_credential(monkeypatch):
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
    assert report == {"passed": False, "runs": [], "workspace_id": workspace_id}
    credential = base + "/provider-credentials/" + credential_id
    assert calls[-2] == ("PATCH", credential, {"enabled": False})
    method, path, body = calls[-1]
    assert (method, path) == ("POST", credential + "/rotate-secret")
    assert body["secret"] and body["secret"] != route.api_key
