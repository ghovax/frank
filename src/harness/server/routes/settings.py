"""Settings routes."""

from __future__ import annotations
from fastapi import APIRouter
from harness.core.background_tasks import spawn_background_task
import harness.core.configuration as _configuration
from fastapi import HTTPException
from harness.core.chatgpt_oauth import ChatGPTLoginFlow
from harness.core.chatgpt_oauth import clear_tokens
from harness.core.chatgpt_oauth import load_tokens
from harness.core.codex_model import clear_subscription_models_cache
from harness.core.codex_model import clear_usage_snapshot
from harness.core.codex_model import fetch_subscription_models
from harness.core.codex_model import get_usage_snapshot
from harness.core.configuration import load_agent_configuration
from harness.core.models import MODELS
from harness.core.models import ModelDefinition
from harness.core.models import available_models
from harness.core.providers import PROVIDERS
import asyncio
from harness.server.models import (
    CompactionUpdateRequest,
    ComputerControlUpdateRequest,
    SandboxUpdateRequest,
    SettingsUpdateRequest,
    UserContextUpdateRequest,
)
from harness.server import boot as _boot
from harness.server import state
from harness.server.services.broadcast import _publish_broadcast
from harness.server.services.sessions import _normalize_permission_mode, _reset_work_habits_acknowledgements
from harness.server.services.agents import _agent_configuration_for_request, _load_agent_sidecar, _recent_models, _save_agent_sidecar
from harness.server.services.settings import _apply_live_credentials, _persist_configuration
from harness.server.services.projects import _reset_all_runtimes

router = APIRouter()


@router.get("/system/full-disk-access")
async def full_disk_access_status():
    """Whether the server process can read Full-Disk-Access-protected data (Screen Time,
    Safari history). Drives the Settings banner + button for the user-context feature."""
    return {"granted": await asyncio.to_thread(_boot._full_disk_access_granted)}


@router.post("/system/full-disk-access/open")
async def open_full_disk_access_settings():
    """Open System Settings to the Full Disk Access pane so the user can add Daisy."""
    await asyncio.to_thread(_boot._open_full_disk_access_settings)
    return {"ok": True}


@router.get("/system/accessibility")
async def accessibility_status():
    """Whether the server can control other apps (read the AX tree, synthesize input) —
    the permission the computer-use tool needs. Drives the Settings banner/button."""
    return {"granted": await asyncio.to_thread(_boot._accessibility_granted)}


@router.post("/system/accessibility/open")
async def open_accessibility_settings():
    """Trigger the system Accessibility prompt and open the pane so the user can grant Daisy."""
    await asyncio.to_thread(_boot._request_accessibility)
    await asyncio.to_thread(_boot._open_accessibility_settings)
    return {"ok": True}


@router.post("/system/screen-recording/open")
async def open_screen_recording_settings():
    """Trigger the system Screen Recording prompt and open the pane so the user can grant Daisy."""
    await asyncio.to_thread(_boot._request_screen_recording)
    await asyncio.to_thread(_boot._open_screen_recording_settings)
    return {"ok": True}


@router.get("/models")
async def list_models_endpoint():
    """The model catalog for the picker: every known model with its provider and
    whether its provider has a resolvable credential (available), plus the provider
    registry. Available models are fronted in the
    picker; locked ones stay listed (greyed) so the user sees what a key unlocks."""
    assert state._global_configuration is not None
    configured_keys = state._global_configuration.configured_provider_keys()
    available_identifiers = {model.identifier for model in available_models(configured_keys)}
    # The chatgpt provider lists the models.dev-derived superset but is only
    # *available* per-model against the account's live subscription catalog, so the
    # picker can grey the ones this plan does not serve. Live models the static
    # filter has not caught (real gpt-* only) are appended so nothing is missed.
    live_chatgpt = await fetch_subscription_models()
    live_slugs = set(live_chatgpt)
    catalog = list(MODELS)
    known_chatgpt_slugs = {
        model.identifier.split("/", 1)[1] for model in MODELS if model.provider == "chatgpt"
    }
    for slug, meta in live_chatgpt.items():
        if slug in known_chatgpt_slugs or not slug.startswith("gpt-"):
            continue
        catalog.append(ModelDefinition(
            identifier=f"chatgpt/{slug}",
            name=meta["name"],
            provider="chatgpt",
            attachment=True,
            vision=True,
            input_modalities=("text", "image"),
            context_length=meta["context"],
        ))

    def _is_available(model: ModelDefinition) -> bool:
        if model.provider == "chatgpt":
            return model.identifier.split("/", 1)[1] in live_slugs
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
    previous = state._chatgpt_login_flow
    if previous is not None:
        await previous.close()
    flow = ChatGPTLoginFlow()
    try:
        await flow.start()
    except OSError as error:
        # Port 1455 is busy — most likely a Codex CLI (or a prior daisy) sign-in.
        raise HTTPException(
            status_code=409,
            detail=f"Could not start sign-in — the callback port 1455 is in use ({error}).",
        ) from error
    state._chatgpt_login_flow = flow

    async def _await_completion() -> None:
        try:
            await flow.wait()
            clear_subscription_models_cache()
            clear_usage_snapshot()
            _reset_all_runtimes()
            _publish_broadcast({"type": "settings_changed"})
        except Exception:  # noqa: BLE001 — timeout/denial just leaves us signed out
            pass
        finally:
            if state._chatgpt_login_flow is flow:
                state._chatgpt_login_flow = None

    spawn_background_task(_await_completion())
    return {"authorize_url": flow.authorize_url}


@router.delete("/auth/chatgpt")
async def chatgpt_auth_signout():
    """Sign out: clear the stored tokens and reset runtimes so the ``chatgpt``
    provider re-locks immediately."""
    if state._chatgpt_login_flow is not None:
        await state._chatgpt_login_flow.close()
        state._chatgpt_login_flow = None
    await asyncio.to_thread(clear_tokens)
    clear_subscription_models_cache()
    clear_usage_snapshot()
    _reset_all_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"ok": True}


@router.get("/settings")
async def get_settings():
    """Return the API credentials stored in ~/.daisy/configuration.yaml so the
    settings dialog can pre-fill them, including per-provider keys."""
    assert state._global_configuration is not None
    try:
        default_agent_configuration = load_agent_configuration(
            state._global_configuration.default_agent,
            state._global_configuration.agent_directories(),
        )
        permission_mode = _normalize_permission_mode(default_agent_configuration.permission_mode)
    except FileNotFoundError:
        # No default agent is installed (a fresh machine before seeding, or a stale
        # default_agent id). Settings must still load — the credentials it holds are
        # independent of any agent — so fall back to the default permission mode.
        permission_mode = _normalize_permission_mode("")
    return {
        "permission_mode": permission_mode,
        "exa_api_key": state._global_configuration.exa.api_key,
        "composio_api_key": state._global_configuration.composio.api_key,
        "jina_api_key": state._global_configuration.jina.api_key,
        "firecrawl_api_key": state._global_configuration.firecrawl.api_key,
        "web_fetch_proxy_url": state._global_configuration.web_fetch.proxy_url,
        "sandbox_enabled": state._global_configuration.sandbox.enabled,
        "workspace_strategy": state._global_configuration.workspace.strategy,
        "compaction": state._global_configuration.compaction.model_dump(),
        "user_context_enabled": state._global_configuration.user_context.enabled,
        "computer_control_enabled": state._global_configuration.computer_control.enabled,
        "providers": {
            identifier: {"api_key": credential.api_key, "base_url": credential.base_url}
            for identifier, credential in state._global_configuration.providers.items()
        },
    }


@router.post("/settings")
async def update_settings(request: SettingsUpdateRequest):
    """Persist API credentials to ~/.daisy/configuration.yaml and apply them
    live: refresh the in-memory configuration, the Exa client, restart the MCP
    client manager so Composio tools appear/disappear with its key, and drop
    cached agent runtimes so the next turn rebuilds with the new credentials."""
    assert state._global_configuration is not None
    configuration = state._global_configuration
    async with state._configuration_lock:
        await _persist_configuration(
            exa_api_key=request.exa_api_key,
            composio_api_key=request.composio_api_key,
            jina_api_key=request.jina_api_key,
            firecrawl_api_key=request.firecrawl_api_key,
            web_fetch_proxy_url=request.web_fetch_proxy_url,
            sandbox_enabled=request.sandbox_enabled,
            provider_keys=request.provider_keys,
            provider_base_urls=request.provider_base_urls,
            workspace_strategy=request.workspace_strategy,
        )
        # The default (global) approval policy lives on the default agent's sidecar —
        # that is the single source GET /settings reads back. Writing it into the
        # dead configuration.yaml `agent:` key (as the old permission_mode arg did)
        # never round-tripped, so the picker snapped back to the default on the
        # settings_changed refetch. Persist it where it is actually read.
        if request.permission_mode is not None:
            try:
                agent_markdown_path, _ = _agent_configuration_for_request(
                    configuration.default_agent, ""
                )
            except FileNotFoundError:
                agent_markdown_path = None
            if agent_markdown_path is not None:
                sidecar = _load_agent_sidecar(agent_markdown_path)
                sidecar["permissionMode"] = _normalize_permission_mode(request.permission_mode)
                _save_agent_sidecar(agent_markdown_path, sidecar)
                if configuration.default_agent in state._executors:
                    state._executors[configuration.default_agent].reset_runtimes()
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
        if request.sandbox_enabled is not None:
            configuration.sandbox.enabled = request.sandbox_enabled
        if request.workspace_strategy is not None:
            configuration.workspace.strategy = request.workspace_strategy
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
    """Persist and apply the sandbox toggle independently from credentials."""
    assert state._global_configuration is not None
    async with state._configuration_lock:
        await _persist_configuration(sandbox_enabled=request.enabled)
        state._global_configuration.sandbox.enabled = request.enabled
        for executor in state._executors.values():
            executor.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "sandbox_enabled": state._global_configuration.sandbox.enabled}


@router.post("/settings/user-context")
async def update_user_context(request: UserContextUpdateRequest):
    """Persist and apply the opt-in user-context toggle. The snapshot is built into the
    static system prompt, so cached runtimes are dropped to rebuild it on the next turn."""
    assert state._global_configuration is not None
    async with state._configuration_lock:
        setting_changed = state._global_configuration.user_context.enabled != request.enabled
        await _persist_configuration(user_context_enabled=request.enabled)
        state._global_configuration.user_context.enabled = request.enabled
        if setting_changed:
            await asyncio.to_thread(_reset_work_habits_acknowledgements)
            for executor in state._executors.values():
                executor.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "user_context_enabled": state._global_configuration.user_context.enabled}


@router.post("/settings/computer-control")
async def update_computer_control(request: ComputerControlUpdateRequest):
    """Persist and apply the opt-in computer-use toggle. The tool set is built per turn,
    so cached runtimes are dropped to add or remove the `computer` tool on the next turn."""
    assert state._global_configuration is not None
    async with state._configuration_lock:
        await _persist_configuration(computer_control_enabled=request.enabled)
        state._global_configuration.computer_control.enabled = request.enabled
        for executor in state._executors.values():
            executor.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "computer_control_enabled": state._global_configuration.computer_control.enabled}


@router.post("/settings/compaction")
async def update_compaction(request: CompactionUpdateRequest):
    """Persist and apply the Observational-Memory compaction settings (the auto on/off
    trigger and thresholds), independently from credentials. The runtime reads the live
    config each turn, so no runtime reset is needed for the trigger to take effect."""
    assert state._global_configuration is not None
    changes = request.model_dump(exclude_none=True)
    if changes:
        async with state._configuration_lock:
            await _persist_configuration(compaction=changes)
            state._global_configuration.compaction = state._global_configuration.compaction.model_copy(update=changes)
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "compaction": state._global_configuration.compaction.model_dump()}
