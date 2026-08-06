"""The daemon's SQLAlchemy layer: the declarative ``Base``, the ORM records that map the
history database's tables, and the lightweight additive schema-reconciliation applied on
startup.

Split into its own leaf module so the persistence schema is one self-contained thing that
``boot``, the services, and the routes depend on, rather than living amongst the request
handlers.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# The key of the one row in ``interface_preferences``.
SOLE_INTERFACE = "interface"


class SessionRecord(Base):
    """A chat session — one A2A context, and the durable half of what the registry knows.

    There used to be two classes with this name: this one, which indexed sessions for the
    sidebar, and a dataclass in `daemon/registry.py` that held identity, parentage, the
    capability token and the process. Neither was complete — `frank ps` read one and the
    browser listed from the other — and they could disagree.

    They are one table now, because a session that outlives its process has to be *written
    down*, and once it is written down there is nothing left for a second record to hold. The
    volatile half (the process id, whether a turn is in flight, whether it is parked on a
    person) is deliberately absent: it describes a process, and a stored "working" survives the
    kill that made it false.

    The capability token is absent for a different reason — it is derived from the session id
    (`registry.token_for`), so a database read discloses no capability and a woken session gets
    the same token its creator was handed.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # == A2A contextId
    agent: Mapped[str] = mapped_column(String, nullable=False)
    # The session that created this one, empty when a person did. What a subtree reap walks.
    parent: Mapped[str] = mapped_column(String, default="")
    # Does this session still exist? `live` or `ended` — never what it is *doing*.
    lifecycle: Mapped[str] = mapped_column(String, default="live")
    # How it finished, and why, for the record that outlives it.
    outcome: Mapped[str] = mapped_column(String, default="")
    exit_reason: Mapped[str] = mapped_column(Text, default="")
    # What its tool children may touch, resolved and clamped once at creation.
    sandbox: Mapped[str] = mapped_column(Text, default="")
    # The one-time work-habits acknowledgement.
    work_habits_acknowledged_at: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[str] = mapped_column(String, default="")
    # The workspace this session belongs to; the agent may address any of the workspace's locations per tool call.
    workspace_id: Mapped[str] = mapped_column(String, default="")
    # Source path selected in the UI. Project-local agents/skills/instructions are resolved from here.
    working_directory: Mapped[str] = mapped_column(Text, default="")
    # Actual path where shell and file tools run.
    runtime_working_directory: Mapped[str] = mapped_column(Text, default="")
    worktree_strategy: Mapped[str] = mapped_column(Text, default="none")
    worktree_path: Mapped[str] = mapped_column(Text, default="")
    worktree_branch: Mapped[str] = mapped_column(Text, default="")
    source_repository_root: Mapped[str] = mapped_column(Text, default="")
    runtime_repository_root: Mapped[str] = mapped_column(Text, default="")
    worktree_head: Mapped[str] = mapped_column(Text, default="")
    worktree_error: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode: Mapped[str] = mapped_column(Text, default="ask")
    input_draft: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_workspace", "workspace_id"),
        Index("idx_sessions_lifecycle", "lifecycle"),
    )


class MachineRecord(Base):
    """Another Frank this one knows how to reach, and the credential for it.

    The desktop's half of what a phone keeps in its keychain: a set of machines you can jump to,
    rather than the single one whichever window happens to be open. Added from the same
    `frank://pair#…` link `frank reach` prints, so there is one way to describe a machine and both
    clients read it.

    The token is stored, and it is worth being plain about what that means: it is a bearer
    credential with full control of *that* machine, sitting at rest in this machine's database.
    The alternative is asking for the link on every jump, which makes the list a bookmark rather
    than a connection. The file is in the user's own data directory, which is where the OAuth
    credentials already are; anything that can read it can already read those.

    Nothing here reaches out. A row is an address and a key, and following it is a navigation the
    interface performs — see the note on CORS in `frank reach`.
    """

    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    # What this machine is called *here*.
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # The identity of the row: one `frank reach` is one address, so pairing the same machine again replaces its token rather than growing a second entry holding a stale one.
    endpoint: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class WorkspaceRecord(Base):
    """A set of locations, and the sessions that run against them.

    Locations carry the user-facing identity; a workspace has no separate editable metadata.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    # The conversation a client should open when it arrives with nothing else to go on.
    last_session_id: Mapped[str] = mapped_column(String, nullable=False, default="")


class InterfacePreferenceRecord(Base):
    """How the interface should look and where it should open — one row, named columns.

    The colour mode, the language, the workspace to reopen, and whether someone has asked for
    computer control while macOS has not granted Accessibility yet. Every one of these used to
    live in the browser's ``localStorage`` (and, in the desktop app, in a second SQLite database
    of its own), which made "what theme is Frank in" a question with one answer per client and
    no way to reconcile them. They are here for the same reason ``last_session_id`` is on the
    workspace: none of it is a fact about a browser. A tab, the desktop app and the phone are
    three views of one daemon.

    A single row, addressed by a constant id, rather than a table of name/value pairs. The set
    is small, fixed, and typed — a bag of strings would put the schema in the interface's
    keystrokes and make every read a parse.
    """

    __tablename__ = "interface_preferences"

    # There is one interface, so there is one row.
    id: Mapped[str] = mapped_column(String, primary_key=True, default=SOLE_INTERFACE)
    # "system" follows the operating system; "light" and "dark" are the explicit choices.
    color_mode: Mapped[str] = mapped_column(String, nullable=False, default="system")
    # A BCP-47 tag the interface has messages for; empty means it has not been chosen.
    locale: Mapped[str] = mapped_column(String, nullable=False, default="")
    # The workspace a fresh launch reopens. Empty until one has been opened.
    last_workspace_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Set when computer control is asked for while Accessibility is not granted. macOS only exposes the grant to a freshly started daemon, so the request has to outlive the process that took it; the interface completes it after the relaunch.
    computer_control_awaiting_grant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ScheduleRecord(Base):
    """A prompt to run in a workspace on a recurring schedule, with nobody watching.

    The unattended part is the whole of the design. A schedule states its own
    ``permission_mode`` and never inherits one, because inheriting would mean a job written
    against a read-only workspace quietly gaining write access the day someone loosens the
    workspace — and the person who would have noticed is asleep. For the same reason a run
    that hits a permission gate fails rather than waiting: there is no one to approve it, and
    a job that blocks forever is worse than one that reports it could not proceed.

    ``timezone`` is stored beside the cron line rather than assumed, because "nine every
    weekday" means nine *where the person is*, and a machine that moves or observes daylight
    saving would otherwise drift by an hour twice a year without anything looking wrong.

    ``last_session_id`` points at what the last firing produced, so a schedule can be read
    backwards into the conversation it started rather than only forwards into the next one."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(String, nullable=False)
    working_directory: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When the scheduler last acted on this, so a daemon that was down over a firing does not replay every one it missed on the next start — one catch-up run, not a stampede.
    last_fired_at: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_session_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_schedules_workspace", "workspace_id"),)


class LocationRecord(Base):
    """A named place a workspace runs tools in: the home server's own filesystem
    (``kind="local"``) or a remote reached over SSH (``kind="remote"``, referencing a
    ``~/.ssh/config`` host alias). ``permission_mode`` is the one execution policy a
    location carries, and it can only tighten the session's; ``name`` is derived
    from the connection (host alias / folder), not user-entered. The model-facing location
    URI is generated from the resolved connection, not stored (so it can't go stale)."""

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # derived workspace-scoped label
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "local" | "remote"
    host_alias: Mapped[str] = mapped_column(Text, default="")  # SSH alias for remotes
    base_directory: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(Text, default="ask")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_locations_workspace", "workspace_id"),)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

    __tablename__ = "model_history"

    model_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="")
    selected_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_model_history_selected_at", "selected_at"),)


class TerminalStateRecord(Base):
    """Persisted scrollback for a server-owned terminal session."""

    __tablename__ = "terminal_states"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    terminal_key: Mapped[str] = mapped_column(String, primary_key=True)
    working_directory: Mapped[str] = mapped_column(Text, default="")
    scrollback: Mapped[str] = mapped_column(Text, default="")
    # Creation time, used to order a context's terminals into stable tabs; set once on insert and never touched again (unlike updated_at, which moves on every write).
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
