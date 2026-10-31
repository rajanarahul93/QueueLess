"""Per-API-instance fan-out: one Redis Pub/Sub connection, many SSE clients.

Lives on app.state (created in the lifespan; no module state). A channel is subscribed when its
first client joins and unsubscribed when its last one leaves; joining waits for Redis to confirm
the subscription, so a stream that then reads the database cannot miss an event published in
between. Each client has a bounded queue: a client that falls behind is marked and closed
rather than slowing the hub or growing memory. If the Pub/Sub connection drops, the hub
reconnects, resubscribes and tells every client to catch up from the database.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from app.core.errors import RateLimited, ServiceUnavailable

logger = logging.getLogger(__name__)

CLIENT_QUEUE_SIZE = 100
SNAPSHOT_CONCURRENCY = 8  # snapshot and replay builds at once, per instance
BOARD_CONNECTS_PER_MINUTE = 60  # new public board streams per instance (no per-IP limit yet)
SUBSCRIBE_TIMEOUT_SEC = 5.0
CACHE_SIZE = 512
HUB_CHANNEL = "hub:keepalive"  # keeps the Pub/Sub connection subscribed while idle
RESYNC = "resync"  # a queue marker: catch up from the database


@dataclass(eq=False)
class Client:
    channel: str
    user_key: str | None  # None for the anonymous board
    events: asyncio.Queue[dict[str, Any] | str] = field(
        default_factory=lambda: asyncio.Queue(CLIENT_QUEUE_SIZE)
    )
    last_id: int = 0
    overflow: bool = False
    released: bool = False


class Hub:
    def __init__(self, redis_url: str, max_per_user: int, max_total: int) -> None:
        self._redis = Redis.from_url(redis_url)
        self._pubsub: PubSub | None = None
        self._channels: dict[str, set[Client]] = {}
        self._confirmed: dict[str, asyncio.Event] = {}
        self._per_user: Counter[str] = Counter()
        self._total = 0
        self._max_per_user, self._max_total = max_per_user, max_total
        self._board_connects: deque[float] = deque()
        self._reader: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._cache: dict[tuple[Any, ...], asyncio.Future[Any]] = {}
        self.snapshots = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)

    @property
    def connections(self) -> int:
        return self._total

    def connections_of(self, user_key: str) -> int:
        return self._per_user[user_key]

    async def start(self) -> None:
        """Connects in the background: the API starts (and answers 503 on streams) even while
        Redis is down."""
        self._reader = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        for clients in self._channels.values():
            for client in clients:
                client.overflow = True  # every open stream ends at its next step
        if self._pubsub is not None:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        await self._redis.aclose()

    def admit(self, user_key: str | None, board: bool = False) -> Client:
        """Counts a new stream, or raises 429. Every admitted client must be released."""
        if self._total >= self._max_total:
            raise RateLimited(5)
        if user_key is not None and self._per_user[user_key] >= self._max_per_user:
            raise RateLimited(5)
        if board:
            now = time.monotonic()
            while self._board_connects and now - self._board_connects[0] > 60:
                self._board_connects.popleft()
            if len(self._board_connects) >= BOARD_CONNECTS_PER_MINUTE:
                raise RateLimited(int(60 - (now - self._board_connects[0])) + 1)
            self._board_connects.append(now)
        self._total += 1
        if user_key is not None:
            self._per_user[user_key] += 1
        return Client(channel="", user_key=user_key)

    async def join(self, client: Client, channel: str) -> None:
        """Adds the client to a channel; returns once Redis has confirmed the subscription."""
        client.channel = channel
        clients = self._channels.setdefault(channel, set())
        clients.add(client)
        try:  # just after start, or while reconnecting: wait briefly for Redis
            await asyncio.wait_for(self._connected.wait(), SUBSCRIBE_TIMEOUT_SEC)
        except TimeoutError as error:
            raise ServiceUnavailable("Live updates unavailable") from error
        assert self._pubsub is not None
        if channel not in self._confirmed:
            self._confirmed[channel] = asyncio.Event()
            try:
                await self._pubsub.subscribe(channel)
            except RedisError as error:
                raise ServiceUnavailable("Live updates unavailable") from error
        try:
            await asyncio.wait_for(self._confirmed[channel].wait(), SUBSCRIBE_TIMEOUT_SEC)
        except TimeoutError as error:
            raise ServiceUnavailable("Live updates unavailable") from error

    async def release(self, client: Client) -> None:
        """Idempotent: called from the stream's finally and again after the response."""
        if client.released:
            return
        client.released = True
        self._total -= 1
        if client.user_key is not None:
            self._per_user[client.user_key] -= 1
            if self._per_user[client.user_key] <= 0:
                del self._per_user[client.user_key]
        clients = self._channels.get(client.channel)
        if clients is None:
            return
        clients.discard(client)
        if not clients:
            del self._channels[client.channel]
            self._confirmed.pop(client.channel, None)
            if self._pubsub is not None:
                with contextlib.suppress(RedisError):
                    await self._pubsub.unsubscribe(client.channel)

    async def cached(
        self, key: tuple[Any, ...], compute: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:  # Any: each view caches its own shape
        """One computation per key (for example one query per queue per event) shared by all
        clients of this instance."""
        future = self._cache.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_task(compute())
            self._cache[key] = future
            while len(self._cache) > CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))
        return await future

    async def _run(self) -> None:
        await self._reconnect()
        while True:
            try:
                assert self._pubsub is not None  # set by _reconnect
                message = await self._pubsub.get_message(timeout=1.0)
                if message is not None:
                    self._handle(message)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as error:
                logger.warning("pubsub lost error=%s; reconnecting", type(error).__name__)
                self._connected.clear()
                await self._reconnect()

    def _handle(self, message: dict[str, Any]) -> None:
        name = (
            message["channel"].decode()
            if isinstance(message["channel"], bytes)
            else message["channel"]
        )
        if message["type"] == "subscribe":
            event = self._confirmed.get(name)
            if event is not None:
                event.set()
        elif message["type"] == "message":
            data = json.loads(message["data"])
            for client in list(self._channels.get(name, ())):
                self._offer(client, data)

    @staticmethod
    def _offer(client: Client, item: dict[str, Any] | str) -> None:
        try:
            client.events.put_nowait(item)
        except asyncio.QueueFull:
            client.overflow = True  # a slow consumer: its stream closes at its next step

    async def _reconnect(self) -> None:
        backoff = 0.5
        while True:
            try:
                if self._pubsub is not None:
                    with contextlib.suppress(RedisError, OSError):
                        await self._pubsub.aclose()  # type: ignore[no-untyped-call]
                    self._pubsub = None
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(HUB_CHANNEL, *self._channels)
                self._pubsub = pubsub
                self._connected.set()
                break
            except (RedisError, OSError):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
        for event in self._confirmed.values():
            event.set()  # resubscribed; streams now catch up from the database
        for clients in self._channels.values():
            for client in clients:
                self._offer(client, RESYNC)
