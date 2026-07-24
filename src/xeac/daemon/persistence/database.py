"""The server's SQLAlchemy layer: the declarative ``Base``, the ORM records that map the
history database's tables, and the lightweight additive schema-reconciliation applied on
startup.

Split into its own leaf module so the persistence schema is one self-contained thing that
``boot``, the services, and the routes depend on, rather than living amongst the request
handlers.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    """A chat session — one A2A context. Tasks live in the A2A task store; this
    table only indexes sessions for the sidebar."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # == A2A contextId
    agent: Mapped[str] = mapped_column(String, nullable=False)
    # The project this session belongs to; the agent may address any of the project's
    # locations per tool call.
    project_id: Mapped[str] = mapped_column(String, default="")
    # Source path selected in the UI. Project-local agents/skills/instructions
    # are resolved from here.
    working_directory: Mapped[str] = mapped_column(Text, default="")
    # Actual path where shell and file tools run. For Git projects this is a
    # per-session worktree; for non-Git directories it falls back to the source.
    runtime_working_directory: Mapped[str] = mapped_column(Text, default="")
    workspace_strategy: Mapped[str] = mapped_column(Text, default="none")
    workspace_path: Mapped[str] = mapped_column(Text, default="")
    workspace_branch: Mapped[str] = mapped_column(Text, default="")
    source_repository_root: Mapped[str] = mapped_column(Text, default="")
    runtime_repository_root: Mapped[str] = mapped_column(Text, default="")
    workspace_head: Mapped[str] = mapped_column(Text, default="")
    workspace_error: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode: Mapped[str] = mapped_column(Text, default="default")
    input_draft: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_project", "project_id"),
    )


class SessionLifecycleRecord(Base):
    """Durable lifecycle facts for one chat session.

    This row is intentionally independent of ``SessionRecord`` because lifecycle events can
    occur before workspace preparation creates the sidebar record. Live execution machinery
    stays in memory; only facts that must survive runtime reconstruction belong here.
    """

    __tablename__ = "session_lifecycle"

    context_id: Mapped[str] = mapped_column(String, primary_key=True)
    work_habits_acknowledged_at: Mapped[str] = mapped_column(String, default="")


class ProjectRecord(Base):
    """The internal grouping key for a set of locations and their sessions.

    Locations carry the user-facing identity; a project has no separate editable metadata.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class LocationRecord(Base):
    """A named place a project runs tools in: the home server's own filesystem
    (``kind="local"``) or a remote reached over SSH (``kind="remote"``, referencing a
    ``~/.ssh/config`` host alias). ``permission_mode`` is the one execution policy a
    location carries (``read_only`` etc. is enforced per tool call); ``name`` is derived
    from the connection (host alias / folder), not user-entered. The model-facing location
    URI is generated from the resolved connection, not stored (so it can't go stale)."""

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # derived project-scoped label
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "local" | "remote"
    host_alias: Mapped[str] = mapped_column(Text, default="")  # SSH alias for remotes
    base_directory: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(Text, default="default")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_locations_project", "project_id"),)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

    __tablename__ = "model_history"

    model_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="")
    selected_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_model_history_selected_at", "selected_at"),)


class ArtifactVersionRecord(Base):
    """One captured version — a commit on a session branch in a shadow git repo. The
    shadow repo lives under the location's ``~/.xeac/versions`` and is driven with an
    explicit ``--git-dir``/``--work-tree`` so it never touches the user's own ``.git``
    (see ``core/artifact_versioning.py``). This row is the DB index into that git
    history: it lets the timeline list versions across every location without querying a
    (possibly remote) git repo. ``sequence`` is the version's 1-based position on the branch."""

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    context_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(String, default="")
    location_uri: Mapped[str] = mapped_column(String, default="")
    git_directory: Mapped[str] = mapped_column(String, nullable=False)
    work_tree: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str] = mapped_column(String, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    tool_call_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_versions_context", "context_id", "created_at"),)


class ArtifactFileRecord(Base):
    """One file changed in a captured version. This is what powers the file-history view
    (``git log`` reconstructed from the DB): each row ties a ``(context, relative_path)`` to
    the commit that changed it and the blob sha of the new content, so a version's bytes
    can be streamed with ``git cat-file`` from whichever location owns the shadow repo.
    Over-cap files are recorded as placeholders (no ``blob_sha``)."""

    __tablename__ = "artifact_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version_id: Mapped[str] = mapped_column(String, nullable=False)
    context_id: Mapped[str] = mapped_column(String, nullable=False)
    location_uri: Mapped[str] = mapped_column(String, default="")
    git_directory: Mapped[str] = mapped_column(String, nullable=False)
    work_tree: Mapped[str] = mapped_column(String, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    absolute_path: Mapped[str] = mapped_column(Text, default="")
    blob_sha: Mapped[str] = mapped_column(String, default="")
    change_type: Mapped[str] = mapped_column(String, default="M")
    size: Mapped[int] = mapped_column(Integer, default=0)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_files_context_path", "context_id", "relative_path"),)


class ArtifactSurfaceRecord(Base):
    """An artifact the agent explicitly surfaced with ``open_artifact`` — i.e. one that
    earns a tab in the artifacts panel. Capture is silent for *everything* the agent
    writes; surfacing is the curated subset. ``id`` is the stable surface id so
    re-opening the same file updates one tab. For a live external URL (an ``iframe``
    with no local file) there is no version history — ``git_directory``/``relative_path`` are empty
    and ``source`` holds the URL."""

    __tablename__ = "artifact_surfaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    context_id: Mapped[str] = mapped_column(String, nullable=False)
    location_uri: Mapped[str] = mapped_column(String, default="")
    git_directory: Mapped[str] = mapped_column(String, default="")
    work_tree: Mapped[str] = mapped_column(String, default="")
    relative_path: Mapped[str] = mapped_column(String, default="")
    absolute_path: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String, default="image")
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")
    tool_call_id: Mapped[str] = mapped_column(String, default="")
    latest_commit_sha: Mapped[str] = mapped_column(String, default="")
    latest_blob_sha: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_surfaces_context", "context_id", "created_at"),)


class ArtifactAnnotationRecord(Base):
    """Image annotations bound to one specific captured version (git commit sha). Keyed on
    ``(surface, version)`` so a regenerated image (a new commit) never inherits the
    previous version's pins."""

    __tablename__ = "artifact_annotations"

    context_id: Mapped[str] = mapped_column(String, primary_key=True)
    surface_id: Mapped[str] = mapped_column(String, primary_key=True)
    version_id: Mapped[str] = mapped_column(String, primary_key=True)
    annotations: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_annotations_context_updated", "context_id", "updated_at"),)


class TerminalStateRecord(Base):
    """Persisted scrollback for a server-owned terminal session."""

    __tablename__ = "terminal_states"

    context_id: Mapped[str] = mapped_column(String, primary_key=True)
    terminal_key: Mapped[str] = mapped_column(String, primary_key=True)
    working_directory: Mapped[str] = mapped_column(Text, default="")
    scrollback: Mapped[str] = mapped_column(Text, default="")
    # Creation time, used to order a context's terminals into stable tabs; set once on
    # insert and never touched again (unlike updated_at, which moves on every write).
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_terminal_states_updated", "updated_at"),)


def _apply_history_schema(sync_engine) -> None:
    """Make the on-disk schema match the declarative models exactly — the models (and
    their ``__table_args__`` indexes) are the single source of truth. Missing tables and
    indexes are created; any existing table whose columns have drifted from its model (an
    older dev build) is dropped and recreated fresh. There is deliberately no
    backward-compatibility migration path: with no data worth preserving across a schema
    change, "make it proper" means recreate, not hand-patch individual columns."""
    inspector = inspect(sync_engine)
    existing_tables = set(inspector.get_table_names())
    drifted_tables = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        model_columns = {column.name for column in table.columns}
        live_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if model_columns != live_columns:
            drifted_tables.append(table)
    if drifted_tables:
        with sync_engine.begin() as connection:
            for table in drifted_tables:
                connection.execute(text(f"DROP TABLE {table.name}"))
    Base.metadata.create_all(sync_engine)
