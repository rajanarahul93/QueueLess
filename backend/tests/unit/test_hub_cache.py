"""`Hub.cached` is single-flight: one load per key, shared by every client of this instance.

A client going away is normal (a tab closes, a stream reaches its lifetime), and it must not
take the other clients' answer with it: the shared load belongs to all of them, not to whoever
happened to start it. These tests use no Redis; `cached` only touches its own dict, and
`Redis.from_url` does not connect until something asks it to.
"""

import asyncio
import gc
import random
from collections.abc import Awaitable
from typing import Any

import pytest

from app.realtime import hub as hub_module
from app.realtime.hub import CACHE_SIZE, Hub

pytestmark = pytest.mark.anyio


def make_hub() -> Hub:
    return Hub("redis://127.0.0.1:6379/0", max_per_user=5, max_total=10)


async def outcome(awaitable: Awaitable[Any]) -> Any:
    """The value, or the exception as a value. A `CancelledError` that escapes a test reads as
    "the test was cancelled", which hides what actually happened; this keeps it readable."""
    done = await asyncio.gather(awaitable, return_exceptions=True)
    return done[0]


async def test_a_a_cancelled_waiter_does_not_take_the_others_answer() -> None:
    hub = make_hub()
    loading = asyncio.Event()

    async def load() -> str:
        loading.set()
        await asyncio.sleep(0.05)
        return "value"

    first = asyncio.create_task(hub.cached(("k",), load))
    await loading.wait()
    second = asyncio.create_task(hub.cached(("k",), load))
    await asyncio.sleep(0)  # let the second waiter reach its await on the shared load
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    got = await outcome(second)
    assert got == "value", f"the other waiter lost the answer: {got!r}"


async def test_b_a_later_caller_after_a_cancelled_waiter_still_gets_a_value() -> None:
    hub = make_hub()
    loading = asyncio.Event()
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        loading.set()
        await asyncio.sleep(0.05)
        return "value"

    only = asyncio.create_task(hub.cached(("k",), load))
    await loading.wait()
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    # Whether the abandoned load finished or has to run again, the next caller gets the value.
    got = await outcome(asyncio.create_task(hub.cached(("k",), load)))
    assert got == "value", f"a later caller on the same key got {got!r}"
    assert loads <= 2


async def test_c_a_load_abandoned_by_everyone_leaves_no_unretrieved_exception() -> None:
    hub = make_hub()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    loading = asyncio.Event()

    async def load() -> str:
        loading.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("the database went away")

    waiters = [asyncio.create_task(hub.cached(("k",), load)) for _ in range(3)]
    await loading.wait()
    await asyncio.sleep(0)
    for waiter in waiters:
        waiter.cancel()
    for waiter in waiters:
        with pytest.raises(asyncio.CancelledError):
            await waiter
    await asyncio.sleep(0.1)  # the abandoned load finishes here, with nobody left to read it

    async def later() -> str:
        return "fresh"

    got = await outcome(asyncio.create_task(hub.cached(("k",), later)))
    assert got == "fresh", f"a later caller on the same key got {got!r}"
    # "Task exception was never retrieved" reaches the loop's handler, not the warnings module.
    assert unhandled == []
    loop.set_exception_handler(None)


async def test_d_a_failing_load_reaches_every_waiter_and_is_not_cached() -> None:
    hub = make_hub()
    loads = 0
    loading = asyncio.Event()

    async def load() -> str:
        nonlocal loads
        loads += 1
        loading.set()
        await asyncio.sleep(0.01)
        raise RuntimeError("the database went away")

    waiters = [asyncio.create_task(hub.cached(("k",), load)) for _ in range(3)]
    await loading.wait()
    for waiter in waiters:
        with pytest.raises(RuntimeError, match="the database went away"):
            await waiter
    assert loads == 1  # one load for the three of them

    async def works() -> str:
        return "value"

    got = await outcome(asyncio.create_task(hub.cached(("k",), works)))
    assert got == "value", f"the failure was kept: {got!r}"  # the failure is not cached


async def test_e_success_is_loaded_once_and_kept() -> None:
    hub = make_hub()
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.01)
        return "value"

    together = await asyncio.gather(*(hub.cached(("k",), load) for _ in range(5)))
    assert together == ["value"] * 5
    assert loads == 1
    # Today's semantics: a successful load is kept for the life of the hub, with no TTL; only
    # the CACHE_SIZE cap evicts, oldest insertion first. A later caller re-reads it.
    assert await hub.cached(("k",), load) == "value"
    assert loads == 1


async def test_g_an_old_loads_bad_ending_never_evicts_a_newer_load_for_the_same_key() -> None:
    """The cap can evict a key while its load is still running. A newer load then takes that
    key, and the old one's ending must not drop the newer one out of the cache."""
    hub = make_hub()
    failing = asyncio.Event()

    async def fails() -> str:
        failing.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("the database went away")

    first = asyncio.create_task(hub.cached(("k",), fails))
    await failing.wait()

    async def trivial() -> str:
        return "other"

    for index in range(CACHE_SIZE):  # the cap evicts ("k",) first: it was inserted first
        await hub.cached(("filler", index), trivial)

    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.1)
        return "value"

    second = asyncio.create_task(hub.cached(("k",), load))  # a new load takes the key
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="the database went away"):
        await first  # the old load ends badly while the new one is still in flight
    assert await outcome(second) == "value"
    assert await hub.cached(("k",), load) == "value"
    assert loads == 1, f"the newer load was evicted by the older one's ending (ran {loads} times)"


async def test_h_an_evicted_load_that_fails_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap can evict a load that is still running. If it then fails with no waiter left,
    somebody still has to read its exception, or Python reports it as never retrieved."""
    monkeypatch.setattr(hub_module, "CACHE_SIZE", 1)
    hub = make_hub()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    failing = asyncio.Event()

    async def fails() -> str:
        failing.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("the database went away")

    waiter = asyncio.create_task(hub.cached(("k",), fails))
    await failing.wait()
    waiter.cancel()  # nobody is waiting for this load any more
    with pytest.raises(asyncio.CancelledError):
        await waiter
    del waiter

    async def trivial() -> str:
        return "other"

    await hub.cached(("filler",), trivial)  # the cap of 1 evicts ("k",), load still in flight
    assert ("k",) not in hub._cache
    await asyncio.sleep(0.1)  # the evicted load fails here
    gc.collect()
    await asyncio.sleep(0)
    assert unhandled == [], f"the evicted load's failure was never read: {unhandled}"
    loop.set_exception_handler(None)


async def test_f_waiters_cancelled_at_random_never_lose_the_answer() -> None:
    seed = 20261107
    print(f"test_f seed={seed}")
    dice = random.Random(seed)
    rounds = 200
    failures: list[tuple[int, Any, Any]] = []
    for round_number in range(rounds):
        hub = make_hub()
        loads = [0]
        loading = asyncio.Event()

        # Bound as defaults: each round has its own counter and its own event.
        async def load(counter: list[int] = loads, started: asyncio.Event = loading) -> str:
            counter[0] += 1
            started.set()
            await asyncio.sleep(0.005)
            return "value"

        waiters = [asyncio.create_task(hub.cached(("k",), load)) for _ in range(5)]
        await loading.wait()
        doomed = dice.sample(range(5), dice.randint(0, 5))  # including none and all
        for index in doomed:
            await asyncio.sleep(dice.choice([0, 0.001, 0.003]))
            waiters[index].cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        for index, result in enumerate(results):
            if index in doomed:
                continue
            if result != "value":
                failures.append((round_number, index, repr(result)))
        if loads[0] != 1:
            failures.append((round_number, "loads", loads[0]))
    assert failures == [], f"{len(failures)} of {rounds} rounds lost an answer: {failures[:5]}"
