"""Settings routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from frank.base.background_tasks import spawn_background_task
import frank.base.confinement as _confinement
import frank.base.configuration as _configuration
from frank.base.credentials import ChatGPTLoginFlow, clear_tokens, load_tokens
from frank.base.cursor_credentials import CursorLoginFlow
from frank.base.cursor_credentials import clear_tokens as cursor_clear_tokens
from frank.base.cursor_credentials import load_tokens as cursor_load_tokens
from frank.base.cursor_subscription import (
    clear_subscription_models_cache as cursor_clear_subscription_models_cache,
)
from frank.base.cursor_subscription import (
    fetch_subscription_models as cursor_fetch_subscription_models,
)
from frank.base.models import available_models, list_models, ModelDefinition
from frank.base.subscription import (
    clear_subscription_models_cache,
    clear_usage_snapshot,
    fetch_subscription_models,
    get_usage_snapshot,
)
from frank.base.providers import PROVIDERS
import asyncio
from frank.protocol.dtos import (
    CompactionUpdateRequest,
    ComputerControlUpdateRequest,
    SandboxUpdateRequest,
    SettingsUpdateRequest,
    UserContextUpdateRequest,
)
from frank.hub.services import workspaces as _workspaces
from frank.rest.services import filesystem as _system
from frank.hub import state
from frank.hub.services.broadcast import _publish_broadcast
from frank.hub.services.sessions import _normalize_permission_mode, _reset_work_habits_acknowledgements
from frank.hub.services.agents import _recent_models
from frank.hub.services.settings import _apply_live_credentials, _persist_configuration
from frank.hub.services.workspaces import _reset_all_runtimes

router = APIRouter()


def _live_additions(
    provider_identifier: str,
    live: dict[str, dict],
    catalog: list[ModelDefinition],
    routable,
    modalities: tuple[str, ...],
) -> list[ModelDefinition]:
    """Catalog entries for live subscription models the static list does not name.

    A subscription's real model list is an account fact discovered at runtime, and it
    moves faster than any list checked into the tree — so whatever the account serves and
    this version has never heard of still becomes pickable, with the live name and
    context window rather than a guess. Capabilities are the provider's, not the model's:
    the live lists carry no modality metadata, so what a route can carry at all is the
    honest answer, and it is the conservative one."""
    known = {
        model.identifier.split("/", 1)[1]
        for model in catalog
        if model.provider == provider_identifier
    }
    return [
        ModelDefinition(
            identifier=f"{provider_identifier}/{model_id}",
            name=meta["name"],
            provider=provider_identifier,
            attachment=len(modalities) > 1,
            vision="image" in modalities,
            input_modalities=modalities,
            context_length=meta["context"],
        )
        for model_id, meta in live.items()
        if model_id not in known and routable(model_id)
    ]


def _merged_sandbox(current, posted: dict):
    """The stored confinement with a posted patch laid over it, validated as a whole.

    The schema refuses a key it does not define, so a patch naming a setting that no longer
    exists fails here rather than being dropped and reported as saved — which is the point of
    refusing it at all. Answered as a bad request, because that is what it is: the caller sent
    a name this version has no setting for."""
    try:
        return _configuration.SandboxConfiguration.model_validate({**current.model_dump(), **posted})
    except Exception as error:  # noqa: BLE001 — the validator's own message is the useful part
        raise HTTPException(status_code=400, detail=f"invalid sandbox settings: {error}") from error


@router.get("/system/full-disk-access")
async def full_disk_access_status():
    """Whether the daemon process can read Full-Disk-Access-protected data (Screen Time,
    Safari history). Drives the Settings banner + button for the user-context feature."""
    return {"granted": await asyncio.to_thread(_workspaces._full_disk_access_granted)}


@router.post("/system/full-disk-access/open")
async def open_full_disk_access_settings():
    """Open System Settings to the Full Disk Access pane so the user can add Frank."""
    await asyncio.to_thread(_workspaces._open_full_disk_access_settings)
    return {"ok": True}


@router.get("/system/accessibility")
async def accessibility_status():
    """Whether the daemon can control other apps (read the AX tree, synthesize input) —
    the permission the computer-use tool needs. Drives the Settings banner/button."""
    return {"granted": await asyncio.to_thread(_system._accessibility_granted)}


@router.post("/system/accessibility/open")
async def open_accessibility_settings():
    """Trigger the system Accessibility prompt and open the pane so the user can grant Frank."""
    await asyncio.to_thread(_system._request_accessibility)
    await asyncio.to_thread(_system._open_accessibility_settings)
    return {"ok": True}


@router.get("/models")
async def list_models_endpoint():
    """The model catalog for the picker: every known model with its provider and
    whether its provider has a resolvable credential (available), plus the provider
    registry. Available models are fronted in the
    picker; locked ones stay listed (greyed) so the user sees what a key unlocks."""
    assert state.global_configuration is not None
    configured_keys = state.global_configuration.configured_provider_keys()
    available_identifiers = {model.identifier for model in available_models(configured_keys)}
    # The two subscription providers list a static superset but are only *available*
    # per-model against the account's live catalog, so the picker can grey the ones a
    # plan does not serve. Both are asked at once — they are independent network calls
    # and this endpoint is polled.
    live_chatgpt, live_cursor = await asyncio.gather(
        fetch_subscription_models(),
        cursor_fetch_subscription_models(),
    )
    catalog = list_models()
    # Live models the static list has not caught are appended so nothing a plan serves is
    # missed. For chatgpt that means real gpt-* only, because the live Codex catalog also
    # names models this harness does not route; for cursor every entry is routable.
    catalog.extend(_live_additions(
        "chatgpt", live_chatgpt, catalog, lambda slug: slug.startswith("gpt-"), ("text", "image"),
    ))
    # Cursor's agent service takes a text turn, so nothing there claims image input.
    catalog.extend(_live_additions(
        "cursor", live_cursor, catalog, lambda _model_id: True, ("text",),
    ))
    live_by_provider = {"chatgpt": set(live_chatgpt), "cursor": set(live_cursor)}

    def _is_available(model: ModelDefinition) -> bool:
        if (live := live_by_provider.get(model.provider)) is not None:
            return model.identifier.split("/", 1)[1] in live
        return model.identifier in available_identifiers

    models = [
        {
            "id": model.identifier,
            "name": model.name,
            "provider": model.provider,
            "available": _is_available(model),
            "attachment": model.attachment,
            "vision": model.vision,
            "input_modalities": list(model.input_modalities),
            "release_date": model.release_date,
        }
        for model in catalog
    ]
    providers = [
        {
            "id": provider.identifier,
            "name": provider.name,
            "openai_compatible": provider.openai_compatible,
            "credential_id": provider.credential_identifier or provider.identifier,
        }
        for provider in PROVIDERS.values()
    ]
    return {
        "models": models,
        "providers": providers,
    }


@router.get("/models/recent")
async def recent_models():
    """Recently selected models (newest first), mirroring the project history — a
    user can quickly switch back to a model they used before without scrolling the
    full catalog. Each entry is only recorded once it is actually selected."""
    return {"models": await asyncio.to_thread(_recent_models)}


@router.get("/auth/chatgpt")
async def chatgpt_auth_status():
    """Whether a ChatGPT subscription is signed in, and for which account — so the
    settings dialog and the model picker can show sign-in state and unlock the
    ``chatgpt`` provider's models. Also carries the latest rate-limit snapshot (the
    5h + weekly usage windows) captured from turns, or ``None`` until the first turn
    runs after sign-in."""
    tokens = await asyncio.to_thread(load_tokens)
    return {
        "signed_in": tokens is not None,
        "email": tokens.email if tokens else "",
        "usage": get_usage_snapshot() if tokens is not None else None,
    }


@router.post("/auth/chatgpt/start")
async def chatgpt_auth_start():
    """Begin an OAuth sign-in: bind the loopback callback server and return the
    authorize URL for the client to open in a browser. The redirect is caught on
    localhost:1455, tokens are persisted, and runtimes are reset in the background
    task below; the UI learns of completion via the ``settings_changed`` broadcast
    (or by polling GET /auth/chatgpt)."""
    previous = state.chatgpt_login_flow
    if previous is not None:
        await previous.close()
    flow = ChatGPTLoginFlow()
    try:
        await flow.start()
    except OSError as error:
        # Port 1455 is busy — most likely a Codex CLI (or a prior frank) sign-in.
        raise HTTPException(
            status_code=409,
            detail=f"Could not start sign-in — the callback port 1455 is in use ({error}).",
        ) from error
    state.chatgpt_login_flow = flow

    async def _await_completion() -> None:
        try:
            await flow.wait()
            clear_subscription_models_cache()
            clear_usage_snapshot()
            await _reset_all_runtimes()
            _publish_broadcast({"type": "settings_changed"})
        except Exception:  # noqa: BLE001 — timeout/denial just leaves us signed out
            pass
        finally:
            if state.chatgpt_login_flow is flow:
                state.chatgpt_login_flow = None

    spawn_background_task(_await_completion())
    return {"authorize_url": flow.authorize_url}


@router.delete("/auth/chatgpt")
async def chatgpt_auth_signout():
    """Sign out: clear the stored tokens and reset runtimes so the ``chatgpt``
    provider re-locks immediately."""
    if state.chatgpt_login_flow is not None:
        await state.chatgpt_login_flow.close()
        state.chatgpt_login_flow = None
    await asyncio.to_thread(clear_tokens)
    clear_subscription_models_cache()
    clear_usage_snapshot()
    await _reset_all_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"ok": True}


@router.get("/auth/cursor")
async def cursor_auth_status():
    """Whether a Cursor subscription is signed in, and for which account — so the settings
    dialog and the model picker can show sign-in state and unlock the ``cursor``
    provider's models.

    No usage snapshot, unlike the ChatGPT status: Cursor's agent service does not report
    the account's remaining allowance anywhere on the calls this makes, so there is
    nothing to show. It surfaces when the allowance runs out, as a stream error, which is
    the only signal the protocol gives."""
    tokens = await asyncio.to_thread(cursor_load_tokens)
    return {"signed_in": tokens is not None, "account": tokens.account if tokens else ""}


@router.post("/auth/cursor/start")
async def cursor_auth_start():
    """Begin a Cursor sign-in: return the login URL for the client to open in a browser
    and start polling for its completion in the background.

    There is no callback server and no port to bind, because Cursor's flow has no
    redirect — the daemon polls Cursor with the ``uuid`` and verifier it minted until the
    browser side completes. So unlike the ChatGPT sign-in this cannot fail at the start;
    it either completes, is superseded, or times out. The UI learns of completion via the
    ``settings_changed`` broadcast (or by polling GET /auth/cursor)."""
    previous = state.cursor_login_flow
    if previous is not None:
        # A second sign-in supersedes the first, and the first must stop polling rather
        # than race to write a token the user has already replaced.
        await previous.close()
    flow = CursorLoginFlow()
    state.cursor_login_flow = flow

    async def _await_completion() -> None:
        try:
            await flow.wait()
            cursor_clear_subscription_models_cache()
            await _reset_all_runtimes()
            _publish_broadcast({"type": "settings_changed"})
        except Exception:  # noqa: BLE001 — timeout/denial/cancellation just leaves us signed out
            pass
        finally:
            if state.cursor_login_flow is flow:
                state.cursor_login_flow = None

    spawn_background_task(_await_completion())
    return {"authorize_url": flow.authorize_url}


@router.delete("/auth/cursor")
async def cursor_auth_signout():
    """Sign out: clear the stored tokens and reset runtimes so the ``cursor`` provider
    re-locks immediately."""
    # Deferred: resumable conversation state is the model client's, and the REST layer does
    # not import the runtime at module scope. Everything else here lives in ``base``.
    from frank.runtime.models.cursor import clear_resumptions

    if state.cursor_login_flow is not None:
        await state.cursor_login_flow.close()
        state.cursor_login_flow = None
    await asyncio.to_thread(cursor_clear_tokens)
    cursor_clear_subscription_models_cache()
    # Resumable conversation state belongs to the account that produced it, so it goes too.
    clear_resumptions()
    await _reset_all_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"ok": True}


@router.get("/settings")
async def get_settings():
    """Return the API credentials stored in the XDG configuration file so the
    settings dialog can pre-fill them, including per-provider keys."""
    assert state.global_configuration is not None
    # The configured floor, not some agent's own setting: this is what a session gets when its
    # creator does not say, and reading it off a nominated profile would make that profile a
    # default agent in everything but name.
    permission_mode = _normalize_permission_mode(state.global_configuration.agent.permission_mode)
    return {
        "permission_mode": permission_mode,
        "exa_api_key": state.global_configuration.exa.api_key,
        "composio_api_key": state.global_configuration.composio.api_key,
        "jina_api_key": state.global_configuration.jina.api_key,
        "firecrawl_api_key": state.global_configuration.firecrawl.api_key,
        "web_fetch_proxy_url": state.global_configuration.web_fetch.proxy_url,
        "sandbox": state.global_configuration.sandbox.model_dump(mode="json"),
        "sandbox_backend": _confinement.probe(),
        "worktree_strategy": state.global_configuration.workspace.strategy,
        "compaction": state.global_configuration.compaction.model_dump(),
        "user_context_enabled": state.global_configuration.user_context.enabled,
        "computer_control_enabled": state.global_configuration.computer_control.enabled,
        "dictation_enabled": state.global_configuration.dictation.enabled,
        "providers": {
            identifier: {"api_key": credential.api_key, "base_url": credential.base_url}
            for identifier, credential in state.global_configuration.providers.items()
        },
    }


@router.post("/settings")
async def update_settings(request: SettingsUpdateRequest):
    """Persist API credentials to the XDG configuration file and apply them
    live: refresh the in-memory configuration, the Exa client, restart the MCP
    client manager so Composio tools appear/disappear with its key, and drop
    cached agent runtimes so the next turn rebuilds with the new credentials."""
    assert state.global_configuration is not None
    configuration = state.global_configuration
    async with state.configuration_lock:
        await _persist_configuration(
            exa_api_key=request.exa_api_key,
            composio_api_key=request.composio_api_key,
            jina_api_key=request.jina_api_key,
            firecrawl_api_key=request.firecrawl_api_key,
            web_fetch_proxy_url=request.web_fetch_proxy_url,
            sandbox=request.sandbox,
            provider_keys=request.provider_keys,
            provider_base_urls=request.provider_base_urls,
            worktree_strategy=request.worktree_strategy,
            permission_mode=(
                _normalize_permission_mode(request.permission_mode)
                if request.permission_mode is not None else None
            ),
        )
        # `agent.permission_mode` in the configuration file, which is where it is read from
        # too. It used to be written onto a nominated profile's sidecar — which made that one
        # profile the global default, and any agent's own setting a global setting.
        if request.permission_mode is not None:
            configuration.agent.permission_mode = _normalize_permission_mode(request.permission_mode)
            await state.reset_runtimes()
        if request.exa_api_key is not None:
            configuration.exa.api_key = request.exa_api_key
        if request.composio_api_key is not None:
            configuration.composio.api_key = request.composio_api_key
        if request.jina_api_key is not None:
            configuration.jina.api_key = request.jina_api_key
        if request.firecrawl_api_key is not None:
            configuration.firecrawl.api_key = request.firecrawl_api_key
        if request.web_fetch_proxy_url is not None:
            configuration.web_fetch.proxy_url = request.web_fetch_proxy_url
        if request.sandbox is not None:
            configuration.sandbox = _merged_sandbox(configuration.sandbox, request.sandbox)
        if request.worktree_strategy is not None:
            configuration.workspace.strategy = request.worktree_strategy
        # Rebuild the providers map from the posted keys/base URLs, merging so a
        # provider the dialog did not render keeps its stored credential.
        merged_providers = {
            identifier: _configuration.ProviderCredential(
                api_key=credential.api_key, base_url=credential.base_url
            )
            for identifier, credential in configuration.providers.items()
        }
        for provider_identifier, api_key in (request.provider_keys or {}).items():
            existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
            merged_providers[provider_identifier] = existing.model_copy(update={"api_key": api_key})
        for provider_identifier, base_url in (request.provider_base_urls or {}).items():
            existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
            merged_providers[provider_identifier] = existing.model_copy(update={"base_url": base_url})
        configuration.providers = merged_providers
        await _apply_live_credentials()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved"}


@router.post("/settings/sandbox")
async def update_sandbox(request: SandboxUpdateRequest):
    """Persist and apply confinement independently from credentials.

    Only sessions created afterwards get the change. A running session keeps what it was built
    with, exactly as its permission mode does, because a boundary that could be widened underneath
    a live session would not be one."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        state.global_configuration.sandbox = _merged_sandbox(
            state.global_configuration.sandbox, request.sandbox
        )
        await _persist_configuration(sandbox=request.sandbox)
    _publish_broadcast({"type": "settings_changed"})
    return {
        "status": "saved",
        "sandbox": state.global_configuration.sandbox.model_dump(mode="json"),
        "sandbox_backend": _confinement.probe(),
    }


@router.post("/settings/user-context")
async def update_user_context(request: UserContextUpdateRequest):
    """Persist and apply the opt-in user-context toggle. The snapshot is built into the
    static system prompt, so cached runtimes are dropped to rebuild it on the next turn."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        setting_changed = state.global_configuration.user_context.enabled != request.enabled
        await _persist_configuration(user_context_enabled=request.enabled)
        state.global_configuration.user_context.enabled = request.enabled
        if setting_changed:
            await asyncio.to_thread(_reset_work_habits_acknowledgements)
            await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "user_context_enabled": state.global_configuration.user_context.enabled}


@router.post("/settings/computer-control")
async def update_computer_control(request: ComputerControlUpdateRequest):
    """Persist and apply the opt-in computer-use toggle. The tool set is built per turn,
    so cached runtimes are dropped to add or remove the `computer` tool on the next turn."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(computer_control_enabled=request.enabled)
        state.global_configuration.computer_control.enabled = request.enabled
        await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "computer_control_enabled": state.global_configuration.computer_control.enabled}


@router.post("/settings/compaction")
async def update_compaction(request: CompactionUpdateRequest):
    """Persist and apply the Observational-Memory compaction settings (the auto on/off
    trigger and thresholds), independently from credentials. The runtime reads the live
    config each turn, so no runtime reset is needed for the trigger to take effect."""
    assert state.global_configuration is not None
    changes = request.model_dump(exclude_none=True)
    if changes:
        async with state.configuration_lock:
            await _persist_configuration(compaction=changes)
            state.global_configuration.compaction = state.global_configuration.compaction.model_copy(update=changes)
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "compaction": state.global_configuration.compaction.model_dump()}
