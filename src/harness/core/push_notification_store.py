"""A persisted A2A push-notification configuration store.

A client registers a webhook (``pushNotificationConfig/set``) to be told when a task
updates. That registration is durable state — it must survive a server restart, or a
client that registered a webhook would silently stop being notified — so it is persisted
to the shared database rather than held in memory. This implements the A2A SDK's
``PushNotificationConfigStore`` contract, backed by the same engine as the task store.
"""

import json
from typing import Optional

from sqlalchemy import Column, MetaData, String, Table, Text, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from a2a.server.tasks.push_notification_config_store import PushNotificationConfigStore
from a2a.types import PushNotificationConfig

from harness.core.net_trust import UntrustedHostError, assert_public_url
from harness.core.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock


class PersistentPushNotificationConfigurationStore(PushNotificationConfigStore):
    """Persists push-notification configurations so a registered webhook survives a
    restart. One row per (task, configuration), upserted by that pair."""

    def __init__(self, engine: AsyncEngine, *, allow_private_webhooks: bool = False):
        self._engine = engine
        # A client registers the URL the server will POST task updates to. Without a guard a
        # peer could register an internal/loopback webhook and turn the server into a blind
        # SSRF + task-data exfiltration channel, durable across restarts. Registration is
        # refused for a non-public host unless the operator explicitly opts in.
        self._allow_private_webhooks = allow_private_webhooks
        self._metadata = MetaData()
        self._table = Table(
            "push_notification_configurations",
            self._metadata,
            Column("task_id", String, primary_key=True),
            Column("configuration_id", String, primary_key=True),
            Column("configuration", Text),
        )
        self._initialized = False

    async def initialize(self) -> None:
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.run_sync(self._metadata.create_all)
        finally:
            release_sqlite_write_lock(write_lock)
        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def set_info(self, task_id: str, notification_config: PushNotificationConfig) -> None:
        await self._ensure_initialized()
        # Refuse a webhook the server must not be pointed at (internal/loopback) before it is
        # ever persisted or POSTed — the anti-SSRF guard on inbound-influenced fetch targets.
        try:
            assert_public_url(notification_config.url, allow_private=self._allow_private_webhooks)
        except UntrustedHostError as exception:
            raise ValueError(f"push notification webhook refused: {exception}") from exception
        # The SDK defaults an unset configuration id to the task id.
        if notification_config.id is None:
            notification_config.id = task_id
        serialized = json.dumps(notification_config.model_dump(mode="json"))
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                statement = sqlite_insert(self._table).values(
                    task_id=task_id, configuration_id=notification_config.id, configuration=serialized,
                )
                await connection.execute(statement.on_conflict_do_update(
                    index_elements=[self._table.c.task_id, self._table.c.configuration_id],
                    set_={"configuration": serialized},
                ))
        finally:
            release_sqlite_write_lock(write_lock)

    async def get_info(self, task_id: str) -> list[PushNotificationConfig]:
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._table.c.configuration).where(self._table.c.task_id == task_id)
                )
            ).scalars().all()
        return [PushNotificationConfig.model_validate(json.loads(row)) for row in rows]

    async def delete_info(self, task_id: str, config_id: Optional[str] = None) -> None:
        await self._ensure_initialized()
        # The SDK defaults an unset configuration id to the task id (deleting that one),
        # rather than every configuration for the task.
        if config_id is None:
            config_id = task_id
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    delete(self._table).where(
                        self._table.c.task_id == task_id,
                        self._table.c.configuration_id == config_id,
                    )
                )
        finally:
            release_sqlite_write_lock(write_lock)
