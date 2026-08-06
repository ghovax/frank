"""A durable push-notification registration store and its sender, so a webhook survives a restart."""

from __future__ import annotations

import json
import logging
import os
from langmesh.base import environment_variables
from typing import Optional

from sqlalchemy import Column, MetaData, String, Table, Text, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

import httpx

from a2a.server.tasks import BasePushNotificationSender
from a2a.server.tasks.push_notification_config_store import PushNotificationConfigStore
from a2a.types import PushNotificationConfig, Task

from langmesh.base.net_trust import (
    UntrustedHostError,
    assert_public_url,
    pin_to_ip,
    resolve_public_ips,
)
from langmesh.base.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock

logger = logging.getLogger(__name__)


class PersistentPushNotificationConfigurationStore(PushNotificationConfigStore):
    """Persists push configurations, one row per task and configuration, upserted by that pair."""

    def __init__(self, engine: AsyncEngine, *, allow_private_webhooks: bool = False):
        self._engine = engine
        # Refused at registration: a loopback webhook would make the daemon a durable exfiltration channel.
        self._allow_private_webhooks = allow_private_webhooks
        self._metadata = MetaData()
        self._table = Table(
            "push_notification_configurations",
            self._metadata,
            Column("turn_id", String, primary_key=True),
            Column("configuration_id", String, primary_key=True),
            Column("configuration", Text),
        )
        self._initialized = False

    @property
    def allow_private_webhooks(self) -> bool:
        """Whether the operator opted into private webhook targets, read by the sender so both guards agree."""
        return self._allow_private_webhooks

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

    async def set_info(self, turn_id: str, notification_config: PushNotificationConfig) -> None:
        await self._ensure_initialized()
        # Refuse a webhook before it is persisted or POSTed: the anti-SSRF guard on inbound-influenced targets.
        try:
            assert_public_url(notification_config.url, allow_private=self._allow_private_webhooks)
        except UntrustedHostError as exception:
            raise ValueError(f"push notification webhook refused: {exception}") from exception
        # The SDK defaults an unset configuration id to the task id.
        if notification_config.id is None:
            notification_config.id = turn_id
        serialized = json.dumps(notification_config.model_dump(mode="json"))
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                statement = sqlite_insert(self._table).values(
                    turn_id=turn_id, configuration_id=notification_config.id, configuration=serialized,
                )
                await connection.execute(statement.on_conflict_do_update(
                    index_elements=[self._table.c.turn_id, self._table.c.configuration_id],
                    set_={"configuration": serialized},
                ))
        finally:
            release_sqlite_write_lock(write_lock)

    async def get_info(self, turn_id: str) -> list[PushNotificationConfig]:
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._table.c.configuration).where(self._table.c.turn_id == turn_id)
                )
            ).scalars().all()
        return [PushNotificationConfig.model_validate(json.loads(row)) for row in rows]

    async def delete_info(self, turn_id: str, config_id: Optional[str] = None) -> None:
        await self._ensure_initialized()
        # The SDK defaults an unset configuration id to the task id, deleting that one rather than all.
        if config_id is None:
            config_id = turn_id
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    delete(self._table).where(
                        self._table.c.turn_id == turn_id,
                        self._table.c.configuration_id == config_id,
                    )
                )
        finally:
            release_sqlite_write_lock(write_lock)


class PinnedPushNotificationSender(BasePushNotificationSender):
    """POSTs task updates to registered webhooks, re-validating and pinning each delivery, since DNS outlives a registration."""

    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        config_store: PushNotificationConfigStore,
        *,
        allow_private: bool = False,
    ) -> None:
        super().__init__(httpx_client, config_store)
        self._allow_private = allow_private

    async def _dispatch_notification(self, task: Task, push_info: PushNotificationConfig) -> bool:
        url = push_info.url
        try:
            hostname, ips = resolve_public_ips(url, allow_private=self._allow_private)
        except UntrustedHostError as exception:
            logger.warning(
                "refusing push-notification for turn_id=%s to untrusted URL %s: %s",
                task.id, url, exception,
            )
            return False
        # Pin to the verified IP so a rebind cannot swap in a private target — unless a proxy does its own connect.
        proxied = bool(
            os.environ.get(environment_variables.HTTPS_PROXY) or os.environ.get("https_proxy")
            or os.environ.get(environment_variables.ALL_PROXY) or os.environ.get("all_proxy")
        )
        if proxied or not ips:
            post_url, headers, extensions = url, {}, {}
        else:
            post_url, headers, extensions = pin_to_ip(url, ips[0], hostname)
        if push_info.token:
            headers["X-A2A-Notification-Token"] = push_info.token
        try:
            response = await self._client.post(
                post_url,
                json=task.model_dump(mode="json", exclude_none=True),
                headers=headers or None,
                extensions=extensions,
            )
            response.raise_for_status()
        except Exception:
            logger.exception(
                "error sending push-notification for turn_id=%s to URL: %s.", task.id, url,
            )
            return False
        logger.info("push-notification sent for turn_id=%s to URL: %s", task.id, url)
        return True
