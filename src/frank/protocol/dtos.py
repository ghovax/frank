"""HTTP request/response models for the Frank server."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionTitle(BaseModel):
    """Structured schema returned by the title-generation LLM call."""

    title: str = Field(
        description=(
            "A concise imperative phrase starting with a verb, then the action it describes; "
            "normal sentence case (not Title Case), no surrounding quotes, no trailing punctuation."
        ),
    )


class AgentInfo(BaseModel):
    id: str
    name: str
    title: str = ""
    # What the agent is for — shown as the subtitle in the UI's agent picker.
    description: str = ""
    # The agent's resolved ``provider/model`` identifier, or empty when it has none.
    model: str = ""


class AgentBashConfigurationResponse(BaseModel):
    enabled: bool
    background_allowed: bool
    permissions: dict[str, str]


class AgentConfigurationResponse(BaseModel):
    id: str
    name: str
    title: str
    model: str = ""
    provider: str = ""
    reasoning_effort: str = "high"
    # `None` where the card sets no ceiling, which is what most cards do.
    permission_mode: Literal["ask", "automatic"] | None = None
    tools_enabled: list[str]
    tools_disabled: list[str]
    bash: AgentBashConfigurationResponse
    path: str


class AgentBashConfigurationRequest(BaseModel):
    enabled: bool | None = None
    background_allowed: bool | None = None
    permissions: dict[str, str] | None = None


class AgentConfigurationUpdateRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    permission_mode: Literal["ask", "automatic"] | None = None
    tools_enabled: list[str] | None = None
    tools_disabled: list[str] | None = None
    bash: AgentBashConfigurationRequest | None = None


class AgentsList(BaseModel):
    """The agent profiles a folder resolves."""

    agents: list[AgentInfo]


class DirectoryValidationRequest(BaseModel):
    directory: str


class DirectoryRevealRequest(BaseModel):
    path: str


class SessionDraftRequest(BaseModel):
    input_draft: str = ""


class SettingsUpdateRequest(BaseModel):
    exa_api_key: str | None = None
    composio_api_key: str | None = None
    jina_api_key: str | None = None
    firecrawl_api_key: str | None = None
    web_fetch_proxy_url: str | None = None
    permission_mode: Literal["ask", "automatic"] | None = None
    sandbox: dict | None = None
    # Per-provider API keys. Both OpenCode gateways use the key under "opencode".
    provider_keys: dict[str, str] | None = None
    # Base URLs for providers with user-configurable OpenAI-compatible endpoints.
    provider_base_urls: dict[str, str] | None = None
    worktree_strategy: Literal["none", "branch", "worktree"] | None = None


class SandboxUpdateRequest(BaseModel):
    """A change to what tool children may do."""

    sandbox: dict


class UserContextUpdateRequest(BaseModel):
    """Opt-in/out of the personal user-context snapshot in the system prompt."""
    enabled: bool


class ComputerControlUpdateRequest(BaseModel):
    """Opt-in/out of the computer-use tool that controls macOS apps."""
    enabled: bool


class SettingValueRequest(BaseModel):
    """One setting, addressed by the dotted path it is written under in the configuration file."""

    path: str
    value: Any = None


class ToolboxUpdateRequest(BaseModel):
    """Turn a session's own tool profile on or off."""
    enabled: bool


class DictationUpdateRequest(BaseModel):
    """Opt-in/out of transcribing the composer's dictation on this machine."""
    enabled: bool


class CompactionUpdateRequest(BaseModel):
    """Observational-memory compaction settings. Only provided fields are changed."""
    automatic: bool | None = None
    reclaim_at_fraction: float | None = None
    condense_log_at_fraction: float | None = None
    output_reserve_fraction: float | None = None
    recent_working_set_fraction: float | None = None
    verbatim_user_fraction: float | None = None


class MCPToolCallRequest(BaseModel):
    server: str
    tool_name: str
    arguments: dict = {}


class MCPResourceReadRequest(BaseModel):
    server: str
    uri: str


class LocationInput(BaseModel):
    # `name` is not accepted — it is derived from the connection (see _derive_location_name).
    kind: str  # "local" | "remote"
    base_directory: str
    host_alias: str = ""
    permission_mode: str = "ask"


class WorkspaceCreateRequest(BaseModel):
    locations: list[LocationInput] = Field(min_length=1)


class InterfacePreferencesUpdateRequest(BaseModel):
    """A partial change to how the interface looks and where it opens."""

    color_mode: Literal["system", "light", "dark"] | None = None
    locale: str | None = None
    last_workspace_id: str | None = None
    computer_control_awaiting_grant: bool | None = None


class MachineRequest(BaseModel):
    """A machine to remember, as the `frank://pair#…` link `frank reach` prints."""

    link: str


class MachineNameRequest(BaseModel):
    """What to call a machine here."""

    name: str


class WorkspaceLastSessionRequest(BaseModel):
    """Which conversation a workspace was last opened at."""

    session_id: str = ""


class AttachmentReference(BaseModel):
    """A local file the user attached by its real OS path (the Tauri desktop app hands over the path; the sandboxed web build cannot and falls back to /uploads)."""

    path: str
