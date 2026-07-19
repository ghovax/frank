"""HTTP request/response models for the Daisy server.

The Pydantic DTOs the FastAPI routes accept and return. Extracted from the server
application module so a route module depends on the request/response contract directly,
without importing from ``harness.server.app`` (which imports the routes — the cycle that
left these models referenced only as unresolved string annotations before).
"""

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
    # The agent's resolved ``provider/model`` identifier, or empty when it falls
    # Empty means the agent is misconfigured; runtime model selection is per-agent.
    model: str = ""


class AgentBashConfigurationResponse(BaseModel):
    enabled: bool
    background_allowed: bool
    permissions: dict[str, str]


class AgentSpawnConfigurationResponse(BaseModel):
    enabled: bool


class AgentConfigurationResponse(BaseModel):
    id: str
    name: str
    title: str
    model: str = ""
    provider: str = ""
    reasoning_effort: str = "high"
    permission_mode: Literal["default", "auto", "read_only", "bypass"]
    stream_agent_progress: bool
    tools_enabled: list[str]
    bash: AgentBashConfigurationResponse
    spawn_agent: AgentSpawnConfigurationResponse
    path: str


class AgentBashConfigurationRequest(BaseModel):
    enabled: bool | None = None
    background_allowed: bool | None = None
    permissions: dict[str, str] | None = None


class AgentSpawnConfigurationRequest(BaseModel):
    enabled: bool | None = None


class AgentConfigurationUpdateRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    permission_mode: Literal["default", "auto", "read_only", "bypass"] | None = None
    stream_agent_progress: bool | None = None
    tools_enabled: list[str] | None = None
    bash: AgentBashConfigurationRequest | None = None
    spawn_agent: AgentSpawnConfigurationRequest | None = None


class AgentsList(BaseModel):
    agents: list[AgentInfo]
    # The server's configured default agent id, so the UI can fall back to it
    # (rather than an arbitrary first entry) when a folder's agents load.
    defaultAgent: str = ""


class DirectoryValidationRequest(BaseModel):
    directory: str


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class QuestionRequest(BaseModel):
    request_id: str
    # One entry per question, in order: a list of selected labels (plus any
    # custom text the user typed). A skipped question is an empty entry. The
    # runtime returns this verbatim to the tool.
    answers: list[Any]
    # The user dismissed the whole prompt without answering. The tool reports the
    # decline to the model and the turn stops rather than proceeding on a guess.
    declined: bool = False


class SteeringRequest(BaseModel):
    message: str


class DirectoryRevealRequest(BaseModel):
    path: str


class ArtifactAnnotationSaveRequest(BaseModel):
    surface_id: str
    version_id: str  # the version's git commit sha
    annotations: list[dict[str, Any]]
    updated_at: str | None = None


class ArtifactRestoreRequest(BaseModel):
    location_uri: str = ""
    git_directory: str
    work_tree: str
    relative_path: str
    commit_sha: str


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "auto", "read_only", "bypass"]


class SessionDraftRequest(BaseModel):
    input_draft: str = ""


class SettingsUpdateRequest(BaseModel):
    exa_api_key: str | None = None
    composio_api_key: str | None = None
    jina_api_key: str | None = None
    firecrawl_api_key: str | None = None
    web_fetch_proxy_url: str | None = None
    permission_mode: Literal["default", "auto", "read_only", "bypass"] | None = None
    sandbox_enabled: bool | None = None
    # Per-provider API keys (the opencode gateway's key lives under "opencode").
    provider_keys: dict[str, str] | None = None
    # Base URLs for the OpenAI-compatible providers (opencode, custom).
    provider_base_urls: dict[str, str] | None = None
    workspace_strategy: Literal["none", "branch", "worktree"] | None = None


class SandboxUpdateRequest(BaseModel):
    enabled: bool


class UserContextUpdateRequest(BaseModel):
    """Opt-in/out of the personal user-context snapshot in the system prompt."""
    enabled: bool


class ComputerControlUpdateRequest(BaseModel):
    """Opt-in/out of the computer-use tool that controls macOS apps."""
    enabled: bool


class CompactionUpdateRequest(BaseModel):
    """Observational-memory compaction settings. Only provided fields are changed."""
    auto: bool | None = None
    observer_context_fraction: float | None = None
    reflector_observation_fraction: float | None = None
    keep_recent_turns: int | None = None


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
    permission_mode: str = "default"


class ProjectCreateRequest(BaseModel):
    locations: list[LocationInput] = Field(min_length=1)


class AttachmentReference(BaseModel):
    """A local file the user attached by its real OS path (the Tauri desktop app
    hands over the path; the sandboxed web build cannot and falls back to /uploads)."""

    path: str
