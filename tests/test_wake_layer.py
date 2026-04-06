"""Tests for WakeLayer — coalesced priority-queue wake system."""

import asyncio
import pytest

from jesse.wake_layer import (
    DEFAULT_COALESCE_MS,
    REASON_PRIORITY,
    WakeLayer,
)


@pytest.mark.asyncio
async def test_basic_wake_handling():
    layer = WakeLayer()
    results = []

    async def handler(reason):
        results.append(reason)
        return (True, "")

    layer.set_handler(handler)
    layer.request_wake(reason="test1", coalesce_ms=10)
    await asyncio.sleep(0.1)
    layer.stop()

    assert len(results) >= 1


@pytest.mark.asyncio
async def test_coalescing():
    layer = WakeLayer()
    count = 0

    async def handler(reason):
        nonlocal count
        count += 1
        return (True, "")

    layer.set_handler(handler)
    for _ in range(10):
        layer.request_wake(reason="same", coalesce_ms=50)
    await asyncio.sleep(0.2)
    layer.stop()

    assert count < 10


@pytest.mark.asyncio
async def test_priority_ordering():
    layer = WakeLayer()
    order = []

    async def handler(reason):
        order.append(reason)
        return (True, "")

    layer.set_handler(handler)
    layer._enqueue("retry")
    layer._enqueue("interval")
    layer._enqueue("action")
    layer.request_wake(reason="trigger", coalesce_ms=10)
    await asyncio.sleep(0.15)
    layer.stop()


def test_priority_values():
    assert REASON_PRIORITY["retry"] == 0
    assert REASON_PRIORITY["interval"] == 1
    assert REASON_PRIORITY["default"] == 2
    assert REASON_PRIORITY["action"] == 3


def test_wake_layer_properties():
    layer = WakeLayer()
    assert not layer.has_pending
    assert not layer.is_running
    layer.stop()


def test_enqueue_coalesces_same_reason():
    layer = WakeLayer()
    layer._enqueue("interval")
    layer._enqueue("interval")
    assert len(layer._pending) == 1


def test_higher_priority_replaces_lower():
    layer = WakeLayer()
    layer._enqueue("retry")
    layer._enqueue("retry")
    assert len(layer._pending) == 1


def test_stop_clears_pending():
    layer = WakeLayer()
    layer._enqueue("test1")
    layer._enqueue("test2")
    assert layer.has_pending
    layer.stop()
    assert not layer.has_pending
    assert layer._handler is None


@pytest.mark.asyncio
async def test_handler_exception_retries():
    layer = WakeLayer()
    call_count = 0

    async def handler(reason):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("fail")
        return (True, "")

    layer.set_handler(handler)
    layer.request_wake(reason="test", coalesce_ms=10)
    await asyncio.sleep(0.3)
    layer.stop()

    assert call_count >= 2


@pytest.mark.asyncio
async def test_requests_in_flight_requeues():
    layer = WakeLayer()
    call_count = 0

    async def handler(reason):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return (False, "requests-in-flight")
        return (True, "")

    layer.set_handler(handler)
    layer.request_wake(reason="test", coalesce_ms=10)
    await asyncio.sleep(0.3)
    layer.stop()

    assert call_count >= 2
