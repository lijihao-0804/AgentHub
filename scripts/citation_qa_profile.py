"""Provision a citation QA profile with the configured allowance on the authorized API."""

from scripts.aliyun_api_smoke import identifier, request


def create_qa_profile(client, workspace, route, credential_id, *, output_allowance=None):
    """Create a new profile. Explicit experimental limits can only reduce the configured cap."""
    cap = route.max_tokens if output_allowance is None else output_allowance
    if type(cap) is not int or not 1 <= cap <= route.max_tokens:
        raise ValueError("QA output allowance must fit within the configured route")
    profile = request(
        client,
        "POST",
        workspace + "/model-profiles",
        {
            "provider_credential_id": credential_id,
            "model": route.model,
            "temperature": str(route.temperature),
            "max_tokens": cap,
            "timeout_seconds": str(route.timeout_seconds),
            "capabilities": route.capabilities,
        },
    )
    return identifier(profile["id"])
