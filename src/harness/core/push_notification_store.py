"""A persisted A2A push-notification configuration store and its sender.

A client registers a webhook (``pushNotificationConfig/set``) to be told when a task
updates. That registration is durable state — it must survive a server restart, or a
client that registered a webhook would silently stop being notified — so it is persisted
to the shared database rather than held in memory. This implements the A2A SDK's
``PushNotificationConfigStore`` contract, backed by the same engine as the task store.

The paired :class:`PinnedPushNotificationSender` is what actually POSTs a task update to a
registered webhook. Both halves apply the same anti-SSRF guard: the store refuses a private
host at registration, and the sender re-resolves and pins the connection at send time, so a
webhook that was public when registered but rebinds to a loopback/metadata address before it
fires cannot turn a durable config into a blind SSRF channel.
"""

import json
import logging
import os
from typing import Optional

from sqlalchemy import Column, MetaData, String, Table, Text, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

import httpx

from a2a.server.tasks import BasePushNotificationSender
from a2a.server.tasks.push_notification_config_store import PushNotificationConfigStore
from a2a.types import PushNotificationConfig, Task

from harness.core.net_trust import (
    UntrustedHostError,
    assert_public_url,
    pin_to_ip,
    resolve_public_ips,
)
from harness.core.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock

logger = logging.getLogger(__name__)


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

    @property
    def allow_private_webhooks(self) -> bool:
        """Whether the operator opted into private/loopback webhook targets — read by the
        paired sender so its send-time guard matches this store's registration-time guard."""
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


class PinnedPushNotificationSender(BasePushNotificationSender):
    """POSTs task updates to registered webhooks, re-validating and pinning each delivery.

    The SDK's ``BasePushNotificationSender`` POSTs to the stored webhook URL directly. But a
    registration is durable and outlives its DNS: a host that was public when the webhook was
    registered can rebind to a loopback/metadata address before the notification actually
    fires, so the one-time check in :meth:`PersistentPushNotificationConfigurationStore.set_info`
    is not enough on its own. This sender re-resolves the host immediately before each POST,
    refuses a non-public result, and pins the socket to the verified IP — ``Host`` header and
    TLS ``sni_hostname`` preserved — so a same-request rebind cannot redirect the delivery to
    an internal service. It is the outbound-webhook counterpart of the inbound file fetch's
    guard, closing the seam the SDK left open. Pinning is skipped when an egress proxy is
    configured (the proxy does its own DNS/connect), where the resolve-and-reject check still
    runs."""

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
                "Refusing push-notification for task_id=%s to untrusted URL %s: %s",
                task.id, url, exception,
            )
            return False
        # Pin to the verified IP so a rebind between the check above and the socket connect
        # cannot swap in a private target — unless an egress proxy is configured, which does
        # its own DNS/connect (pinning to an IP would then be wrong, and the resolve check
        # above already ran).
        proxied = bool(
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
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
                "Error sending push-notification for task_id=%s to URL: %s.", task.id, url,
            )
            return False
        logger.info("Push-notification sent for task_id=%s to URL: %s", task.id, url)
        return True
