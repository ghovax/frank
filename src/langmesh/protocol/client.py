"""The outbound A2A client: reach third-party agents on someone else's server and hand them work."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import httpx

from langmesh.base.net_trust import UntrustedHostError, assert_public_host

from a2a.client import Client, ClientConfig, ClientEvent, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import AgentCard, Message, TransportProtocol
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from langmesh.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)

def _available_transports() -> list[TransportProtocol]:
    """Transports the client will negotiate, in preference order, with gRPC only when its dependency is installed."""
    transports = [TransportProtocol.jsonrpc, TransportProtocol.http_json]
    try:
        from a2a.client.transports.grpc import GrpcTransport  # noqa: F401

        transports.insert(1, TransportProtocol.grpc)
    except Exception:  # noqa: BLE001 — grpc extra not installed; skip it
        pass
    return transports


@dataclass
class RemoteAgentAuth:
    """How to authenticate to one remote agent: nothing, a static token in a header, or fetched client credentials."""

    kind: str = "none"
    token: str = ""
    header: str = "Authorization"
    scheme_prefix: str = "Bearer"
    # OAuth2 client-credentials.
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = field(default_factory=list)


@dataclass
class RemoteAgentConfiguration:
    """One registered remote agent: the local handle the model delegates to, and the card URL that anchors trust."""

    name: str
    card_url: str
    auth: RemoteAgentAuth = field(default_factory=RemoteAgentAuth)
    card_ttl_seconds: int = 3600
    # Extra hostnames allowed beyond the card's origin, which is always allowed.
    allowed_hosts: list[str] = field(default_factory=list)
    # Opt in to private/loopback/link-local targets (e.g. a LangMesh-to-LangMesh loopback test).
    allow_private: bool = False
    # Local agent profiles permitted to delegate to this remote agent. Empty = all.
    allowed_profiles: list[str] = field(default_factory=list)


class RemoteAgentTrustError(Exception):
    """A remote card's URL failed the host-trust check."""


class _OAuth2ClientCredentials(httpx.Auth):
    """An httpx auth flow that keeps a valid client-credentials bearer token, refreshing it before expiry."""

    def __init__(self, auth: RemoteAgentAuth):
        self._auth = auth
        self._token = ""
        self._expiry = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request):
        token = await self._ensure_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def _ensure_token(self) -> str:
        # A 30s skew guards against using a token that expires mid-flight.
        if self._token and time.monotonic() < self._expiry - 30:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expiry - 30:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self._auth.scopes:
                data["scope"] = " ".join(self._auth.scopes)
            # No redirects on the token endpoint, since one could replay the credentials to a host the configuration never named.
            async with httpx.AsyncClient(timeout=active_tuning().duration(Tunable.card_resolve), follow_redirects=False) as client:
                response = await client.post(
                    self._auth.token_url,
                    data=data,
                    auth=(self._auth.client_id, self._auth.client_secret),
                )
                response.raise_for_status()
                payload = response.json()
            self._token = str(payload.get("access_token", ""))
            self._expiry = time.monotonic() + float(payload.get("expires_in", 3600))
            return self._token


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _assert_url_trusted(url: str, configuration: RemoteAgentConfiguration) -> None:
    """Refuse a URL that leaves the registered origin or resolves into a private range."""
    host = _host_of(url)
    if not host:
        raise RemoteAgentTrustError(f"Remote agent {configuration.name!r}: malformed URL {url!r}.")
    allowed = {_host_of(configuration.card_url), *(host.lower() for host in configuration.allowed_hosts)}
    if host not in allowed:
        raise RemoteAgentTrustError(
            f"Remote agent {configuration.name!r}: card URL host {host!r} is not the registered "
            f"origin {_host_of(configuration.card_url)!r} (and not in allowed_hosts)."
        )
    try:
        assert_public_host(host, allow_private=configuration.allow_private)
    except UntrustedHostError as exception:
        # The inner error already names the remedy, so repeating it printed the same hint twice.
        raise RemoteAgentTrustError(
            f"Remote agent {configuration.name!r}: {exception}."
        ) from exception


def _card_urls(card: AgentCard) -> list[str]:
    urls = [card.url]
    for interface in (card.additional_interfaces or []):
        urls.append(interface.url)
    return [url for url in urls if url]


class _RemoteAgent:
    """Live state for one remote agent, kept per agent so two never share a client or a credential."""

    def __init__(self, configuration: RemoteAgentConfiguration):
        self.configuration = configuration
        self.card: Optional[AgentCard] = None
        self.card_fetched_at = 0.0
        self.health = "unresolved"  # unresolved | ok | unreachable | untrusted
        self.error = ""
        self._httpx: Optional[httpx.AsyncClient] = None
        self._client: Optional[Client] = None

    def _httpx_client(self) -> httpx.AsyncClient:
        if self._httpx is None:
            headers: dict[str, str] = {}
            auth_flow: Optional[httpx.Auth] = None
            auth = self.configuration.auth
            if auth.kind in {"bearer", "api_key"} and auth.token:
                value = f"{auth.scheme_prefix} {auth.token}".strip() if auth.kind == "bearer" else auth.token
                headers[auth.header] = value
            elif auth.kind == "oauth2":
                auth_flow = _OAuth2ClientCredentials(auth)
            self._httpx = httpx.AsyncClient(
                headers=headers or None,
                auth=auth_flow,
                timeout=active_tuning().duration(Tunable.card_resolve),
                follow_redirects=False,  # a redirect could bounce us off the trusted origin
            )
        return self._httpx

    async def resolve_card(self, *, force: bool = False) -> Optional[AgentCard]:
        """Fetch or reuse a card, enforcing host trust on every URL it advertises and keeping the previous one on failure."""
        ttl = max(0, self.configuration.card_ttl_seconds)
        fresh = self.card is not None and (time.monotonic() - self.card_fetched_at) < ttl
        if fresh and not force:
            return self.card
        base = self.configuration.card_url
        # Split the well-known URL into base + path for the resolver.
        parsed = urlparse(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or AGENT_CARD_WELL_KNOWN_PATH
        try:
            _assert_url_trusted(base, self.configuration)
            resolver = A2ACardResolver(self._httpx_client(), origin, agent_card_path=path)
            card = await asyncio.wait_for(resolver.get_agent_card(), timeout=active_tuning().duration(Tunable.card_resolve))
            for url in _card_urls(card):
                _assert_url_trusted(url, self.configuration)
        except RemoteAgentTrustError as exception:
            self.health, self.error = "untrusted", str(exception)
            logger.warning("remote agent %r untrusted", self.configuration.name, exc_info=True)
            return None
        except Exception as exception:  # noqa: BLE001 — any resolution failure is non-fatal
            self.health, self.error = "unreachable", str(exception)
            logger.warning("remote agent %r unreachable", self.configuration.name, exc_info=True)
            return None
        self.card = card
        self.card_fetched_at = time.monotonic()
        self.health, self.error = "ok", ""
        self._client = None  # a new card may change transport/URL; rebuild the client lazily
        return card

    async def client(self) -> Optional[Client]:
        card = await self.resolve_card()
        if card is None:
            return None
        if self._client is None:
            config = ClientConfig(
                httpx_client=self._httpx_client(),
                supported_transports=_available_transports(),
                streaming=bool(card.capabilities and card.capabilities.streaming),
            )
            self._client = ClientFactory(config).create(card)
            # Fetch the richer authenticated card now that the client carries auth, re-checking trust on every URL.
            if getattr(card, "supports_authenticated_extended_card", False):
                try:
                    extended = await self._client.get_card()
                    for url in _card_urls(extended):
                        _assert_url_trusted(url, self.configuration)
                    self.card = extended
                except RemoteAgentTrustError as exception:
                    # A trust violation on the extended card is a real signal, so it is surfaced rather than silently ignored.
                    self.health, self.error = "untrusted", str(exception)
                    logger.warning("remote agent %r extended card untrusted", self.configuration.name, exc_info=True)
                except Exception:  # noqa: BLE001 — a non-trust extended-card fetch failure is optional
                    pass
        return self._client

    async def aclose(self) -> None:
        if self._httpx is not None:
            with_suppress = self._httpx.aclose()
            try:
                await with_suppress
            except Exception:  # noqa: BLE001
                pass
        self._httpx = None
        self._client = None


class RemoteAgentManager:
    """Facade over the registered remote agents, resolving cards at start and applying a new configuration set live."""

    def __init__(self, configurations: Optional[dict[str, RemoteAgentConfiguration]] = None):
        self._agents: dict[str, _RemoteAgent] = {
            name: _RemoteAgent(configuration)
            for name, configuration in (configurations or {}).items()
        }

    def names(self) -> list[str]:
        return sorted(self._agents)

    def is_remote(self, name: str) -> bool:
        return name in self._agents

    def is_allowed_for(self, name: str, profile: str) -> bool:
        """Whether a local profile may delegate to this agent, where an empty allow-list permits all."""
        agent = self._agents.get(name)
        if agent is None:
            return False
        allowed = agent.configuration.allowed_profiles
        return not allowed or profile in allowed

    def configuration(self, name: str) -> Optional[RemoteAgentConfiguration]:
        agent = self._agents.get(name)
        return agent.configuration if agent is not None else None

    def card(self, name: str) -> Optional[AgentCard]:
        agent = self._agents.get(name)
        return agent.card if agent is not None else None

    def health(self, name: str) -> dict[str, str]:
        agent = self._agents.get(name)
        if agent is None:
            return {"health": "unknown", "error": ""}
        return {"health": agent.health, "error": agent.error}

    async def start(self) -> None:
        """Resolve every agent's card once, isolating failures so one bad agent never blocks the others."""
        await asyncio.gather(*(agent.resolve_card() for agent in self._agents.values()), return_exceptions=True)

    async def refresh(self, name: str) -> Optional[AgentCard]:
        agent = self._agents.get(name)
        return await agent.resolve_card(force=True) if agent is not None else None

    async def refresh_all(self) -> None:
        """Force a fresh card resolution for every agent, updating health even while idle."""
        await asyncio.gather(*(agent.resolve_card(force=True) for agent in self._agents.values()), return_exceptions=True)

    def has_agents(self) -> bool:
        return bool(self._agents)

    async def reconcile(self, configurations: dict[str, RemoteAgentConfiguration]) -> None:
        """Apply a new configuration set live: drop what changed, keep what did not, resolve what is new."""
        for name, agent in list(self._agents.items()):
            if name not in configurations or configurations[name] != agent.configuration:
                await agent.aclose()
                self._agents.pop(name, None)
        for name, configuration in configurations.items():
            if name not in self._agents:
                self._agents[name] = _RemoteAgent(configuration)
        await self.start()

    async def message_session(self, name: str, message: Message) -> AsyncIterator[ClientEvent | Message]:
        """Stream a message to a remote agent, yielding the client's updates."""
        agent = self._agents.get(name)
        if agent is None:
            raise LookupError(f"No remote agent named {name!r}.")
        client = await agent.client()
        if client is None:
            raise RuntimeError(f"Remote agent {name!r} is not reachable ({agent.health}: {agent.error}).")
        async for event in client.message_session(message):
            yield event

    async def aclose(self) -> None:
        await asyncio.gather(*(agent.aclose() for agent in self._agents.values()), return_exceptions=True)
