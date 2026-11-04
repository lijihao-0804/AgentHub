"""Per-attempt guarded HTTP clients for the two supported SDK paths.

The socket uses an authorized numeric address; httpcore retains the original
origin for Host, TLS SNI and certificate verification. No provider request can
select a proxy, follow a redirect or open a socket outside this boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx

from packages.model_gateway.egress import (
    TARGET_FORBIDDEN,
    ProviderEgressError,
    ProviderEgressPolicy,
    ProviderEndpoint,
    effective_provider_base_url,
    parse_provider_endpoint,
)


def _peer_address(stream: httpcore.AsyncNetworkStream) -> str | None:
    try:
        peer = stream.get_extra_info("server_addr")
    except Exception:
        return None
    if isinstance(peer, tuple | list) and peer:
        return str(peer[0])
    return peer if isinstance(peer, str) else None


class _PinnedProviderBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, endpoint, policy, delegate=None):
        self.endpoint = endpoint
        self.policy = policy
        self.delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.endpoint.host or port != self.endpoint.port:
            raise ProviderEgressError(TARGET_FORBIDDEN)
        async with asyncio.timeout(timeout):
            authorization = await self.policy.authorize(self.endpoint)
            for index, address in enumerate(authorization.addresses):
                try:
                    stream = await self.delegate.connect_tcp(
                        address,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout):
                    if index + 1 == len(authorization.addresses):
                        raise
                    # No HTTP request bytes have reached a provider. Trying
                    # another already-authorized TCP address is not a model retry.
                    continue
                try:
                    peer = authorization.authorize_peer(_peer_address(stream))
                    if peer != address:
                        raise ProviderEgressError(TARGET_FORBIDDEN)
                except BaseException:
                    await stream.aclose()
                    raise
                return stream
        raise ProviderEgressError(TARGET_FORBIDDEN)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise ProviderEgressError(TARGET_FORBIDDEN)

    async def sleep(self, seconds):
        await self.delegate.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class GuardedProviderTransport(httpx.AsyncBaseTransport):
    """Pin before send and refuse redirects even if an SDK requests them."""

    def __init__(self, endpoint, policy, *, network_backend=None):
        self.endpoint = endpoint
        self.blocked_failure_code: str | None = None
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=_PinnedProviderBackend(endpoint, policy, network_backend),
        )

    async def handle_async_request(self, request):
        try:
            target = parse_provider_endpoint(str(request.url))
            if (target.scheme, target.host, target.port) != (
                self.endpoint.scheme,
                self.endpoint.host,
                self.endpoint.port,
            ):
                raise ProviderEgressError(TARGET_FORBIDDEN)
            response = await self._pool.handle_async_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
            )
            if 300 <= response.status < 400:
                await response.aclose()
                raise ProviderEgressError(TARGET_FORBIDDEN)
        except ProviderEgressError as error:
            self.blocked_failure_code = error.failure_code
            raise
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self._pool.aclose()


TransportFactory = Callable[[ProviderEndpoint, ProviderEgressPolicy], GuardedProviderTransport]


@dataclass(frozen=True)
class GuardedSDKClient:
    client: Any
    transport: GuardedProviderTransport
    base_url: str


@asynccontextmanager
async def guarded_sdk_client(
    provider: str,
    base_url: str | None,
    secret: str,
    timeout: float,
    *,
    policy: ProviderEgressPolicy,
    transport_factory: TransportFactory | None = None,
) -> AsyncIterator[GuardedSDKClient]:
    endpoint = parse_provider_endpoint(effective_provider_base_url(provider, base_url))
    transport = (
        transport_factory(endpoint, policy)
        if transport_factory is not None
        else GuardedProviderTransport(endpoint, policy)
    )
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    ) as http_client:
        if provider.strip().lower().replace("_", "-") == "deepseek":
            from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

            class GuardedHTTPHandler(AsyncHTTPHandler):
                # Do not call the base constructor: it creates an unguarded
                # proxy-aware client before a caller can replace it.
                def __init__(self):
                    self.timeout = timeout
                    self.event_hooks = None
                    self.ssl_verify = True
                    self.shared_session = None
                    self._owns_client = False
                    self._client = http_client
                    self.client_alias = "agenthub-guarded-provider"

                def create_client(self, *args, **kwargs):
                    if http_client.is_closed:
                        raise ProviderEgressError(TARGET_FORBIDDEN)
                    return http_client

            sdk = GuardedHTTPHandler()
        else:
            from openai import AsyncOpenAI

            sdk = AsyncOpenAI(
                api_key=secret,
                base_url=endpoint.url,
                max_retries=0,
                timeout=timeout,
                http_client=http_client,
            )
        yield GuardedSDKClient(sdk, transport, endpoint.url)
