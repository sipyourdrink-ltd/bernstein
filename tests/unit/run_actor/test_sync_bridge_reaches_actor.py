"""The sync bridge actually delivers into a started actor (#4899).

``run_actor_registry`` exists so synchronous writers - file-driven CLI helpers,
subprocess shims - can publish into a live :class:`RunActor` by session id.
Nothing ever called ``register``, so the registry was empty for the life of
every process and ``publish_event_sync`` returned ``False`` on every call:
``approval_gate``'s parallel emit had never emitted anything.

Two things hid it, and they compound. The no-op is documented as safe, so the
broken state and the intended steady state are byte-identical from the caller's
side - ``False`` means both "no actor here" and "this has never worked". And
``test_approval_gate_dark_paths.py`` patches ``publish_event_sync`` at six call
sites, so the coverage is real, the assertion is true, and nothing ever comes
out the other end.

So these drive a REAL actor. A mock is what covered this for however long it
was here, and another one would cover it again.
"""

from __future__ import annotations

import asyncio

import pytest

from bernstein.core.orchestration import run_actor_registry
from bernstein.core.orchestration.run_actor import Event, RunActor


@pytest.mark.asyncio
async def test_a_started_actor_receives_a_sync_published_event() -> None:
    """The whole point of the bridge, asserted end to end."""
    actor = RunActor("sess-bridge")
    await actor.start()
    try:
        assert run_actor_registry.publish_event_sync("sess-bridge", Event(kind="watchdog_tick", source="test")) is True
        # The submit is scheduled on the actor's loop, not awaited by the caller - that is what
        # makes it safe from a sync context. Yield until the writer has drained it.
        for _ in range(100):
            if actor.snapshot().last_seq > 0:
                break
            await asyncio.sleep(0)
        assert actor.snapshot().last_seq > 0, "the event was scheduled but never applied"
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_starting_an_actor_registers_it_under_its_session_id() -> None:
    """Registration is keyed by session id, which is how a sync writer addresses it."""
    actor = RunActor("sess-registered")
    await actor.start()
    try:
        assert run_actor_registry.get("sess-registered") is actor
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_a_stopped_actor_is_unreachable_rather_than_merely_draining() -> None:
    """`stop` leaves the registry, so nothing can publish into an actor on its way down."""
    actor = RunActor("sess-stopped")
    await actor.start()
    await actor.stop()
    assert run_actor_registry.get("sess-stopped") is None
    assert run_actor_registry.publish_event_sync("sess-stopped", Event(kind="watchdog_tick", source="test")) is False


@pytest.mark.asyncio
async def test_an_unknown_session_still_returns_false() -> None:
    """The documented safe no-op survives: a miss is a miss, not an error.

    This is the behaviour the bridge was designed around, and wiring registration must not turn
    an absent actor into an exception on a legacy write path.
    """
    assert (
        run_actor_registry.publish_event_sync("sess-never-existed", Event(kind="watchdog_tick", source="test")) is False
    )


@pytest.mark.asyncio
async def test_two_actors_do_not_collide() -> None:
    """Each session addresses its own actor; a shared registry must key them apart."""
    first = RunActor("sess-a")
    second = RunActor("sess-b")
    await first.start()
    await second.start()
    try:
        assert run_actor_registry.get("sess-a") is first
        assert run_actor_registry.get("sess-b") is second
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.asyncio
async def test_a_superseded_actor_stopping_leaves_the_live_one_reachable() -> None:
    """A reconnect registers a second actor under the same session id.

    The first actor stopping afterwards must not detach the second: the
    registry keys by session, so an unconditional pop would make the live
    actor unreachable and silently send every later event down the legacy
    path.
    """
    first = RunActor("sess-reconnect")
    second = RunActor("sess-reconnect")
    await first.start()
    await second.start()
    try:
        assert run_actor_registry.get("sess-reconnect") is second
        await first.stop()
        assert run_actor_registry.get("sess-reconnect") is second
        assert (
            run_actor_registry.publish_event_sync(
                "sess-reconnect",
                Event(kind="watchdog_tick", source="test"),
            )
            is True
        )
    finally:
        await second.stop()


def test_publishing_into_a_dead_loop_retires_the_entry() -> None:
    """An actor whose loop closed without stop() must not be pinned forever.

    Registration only became reachable once actors register themselves, so
    this is the first point at which a leak is possible: the entry holds the
    actor, its replay buffer, and the dead loop.
    """

    async def _start() -> RunActor:
        actor = RunActor("sess-dead-loop")
        await actor.start()
        return actor

    loop = asyncio.new_event_loop()
    try:
        actor = loop.run_until_complete(_start())
    finally:
        loop.close()

    assert run_actor_registry.get("sess-dead-loop") is actor
    assert (
        run_actor_registry.publish_event_sync(
            "sess-dead-loop",
            Event(kind="watchdog_tick", source="test"),
        )
        is False
    )
    assert run_actor_registry.get("sess-dead-loop") is None
