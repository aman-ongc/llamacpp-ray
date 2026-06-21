import asyncio

import pytest

from gateway.router import NodePool, PoolBusyError


def _pool(parallel=1, max_queued=2, ips=None, healthy=None) -> NodePool:
    ips = ips or ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    healthy = healthy if healthy is not None else set(ips)
    return NodePool(ips, port=8080, parallel=parallel, max_queued=max_queued, healthy=healthy)


async def test_affinity_is_deterministic():
    pool = _pool()
    key = "sk-ongc-abc123"
    assert pool.affinity_node(key) == pool.affinity_node(key)


async def test_affinity_maps_to_known_node():
    pool = _pool()
    assert pool.affinity_node("sk-ongc-abc123") in pool.ips


async def test_affinity_falls_back_to_healthy_subset():
    pool = _pool(ips=["a", "b", "c"], healthy={"b", "c"})
    # Whichever key hashes to "a" should fall back to a healthy node instead.
    for key in (f"key{i}" for i in range(50)):
        assert pool.affinity_node(key) in {"b", "c"}


async def test_affinity_returns_none_when_nothing_healthy():
    pool = _pool(ips=["a", "b"], healthy=set())
    assert pool.affinity_node("any-key") is None


async def test_acquire_picks_least_loaded_node():
    pool = _pool(parallel=2)
    ip1 = await pool.acquire(exclude=set())
    # ip1 now has in_flight=1; the other two are at 0, so the next acquire
    # without affinity should land on one of the still-idle nodes.
    ip2 = await pool.acquire(exclude=set())
    assert ip2 != ip1 or pool.in_flight[ip1] <= pool.in_flight[ip2]
    assert pool.in_flight[ip1] >= 1


async def test_acquire_respects_exclude():
    pool = _pool(ips=["a", "b"], parallel=5)
    ip = await pool.acquire(exclude={"a"})
    assert ip == "b"


async def test_acquire_prefers_affinity_node_when_available():
    pool = _pool(ips=["a", "b", "c"], parallel=5)
    ip = await pool.acquire(exclude=set(), affinity_ip="b")
    assert ip == "b"


async def test_pool_full_raises_pool_busy_beyond_max_queued():
    pool = _pool(ips=["a"], parallel=1, max_queued=0)
    await pool.acquire(exclude=set())  # fills the only node's only slot
    with pytest.raises(PoolBusyError):
        await pool.acquire(exclude=set())


async def test_release_wakes_a_queued_waiter():
    pool = _pool(ips=["a"], parallel=1, max_queued=1)
    ip = await pool.acquire(exclude=set())  # node "a" now full

    waiter_done = asyncio.Event()
    result: dict[str, str] = {}

    async def waiter():
        result["ip"] = await pool.acquire(exclude=set())
        waiter_done.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not waiter_done.is_set()  # still queued — node "a" is full

    await pool.release(ip)
    await asyncio.wait_for(waiter_done.wait(), timeout=1)
    assert result["ip"] == "a"
    await task


async def test_evicted_node_not_chosen_again_until_restored():
    pool = _pool(ips=["a", "b"], parallel=5)
    pool.evict("a")
    for _ in range(10):
        ip = await pool.acquire(exclude=set())
        assert ip == "b"
        await pool.release(ip)
    # Restoring health makes it a candidate again.
    pool.healthy.add("a")
    seen = set()
    for _ in range(10):
        ip = await pool.acquire(exclude=set())
        seen.add(ip)
        await pool.release(ip)
    assert "a" in seen


async def test_unhealthy_node_not_used_as_fallback_when_healthy_peers_saturate():
    """A node that's known-unhealthy must never be picked just because its
    healthy peers are all busy — that's the exact Ray-scheduler bias (an
    instantly-failing node looks idle and gets MORE traffic, not less) this
    router is designed to avoid. The correct behavior is to queue."""
    pool = _pool(ips=["a", "b"], parallel=1, max_queued=5)
    pool.evict("a")
    ip = await pool.acquire(exclude=set())
    assert ip == "b"  # only healthy node, takes the only request

    async def second_acquire():
        return await pool.acquire(exclude=set())

    task = asyncio.create_task(second_acquire())
    await asyncio.sleep(0.05)
    assert not task.done()  # must be queued, not routed to unhealthy "a"

    await pool.release(ip)
    second_ip = await asyncio.wait_for(task, timeout=1)
    assert second_ip == "b"


async def test_concurrent_dispatch_to_same_node_when_parallel_gt_one():
    pool = _pool(ips=["a"], parallel=2, max_queued=0)
    ip1 = await pool.acquire(exclude=set())
    ip2 = await pool.acquire(exclude=set())  # should not block — parallel=2
    assert ip1 == ip2 == "a"
    assert pool.in_flight["a"] == 2
    with pytest.raises(PoolBusyError):
        await pool.acquire(exclude=set())
