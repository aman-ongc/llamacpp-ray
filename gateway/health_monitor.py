import asyncio
import logging
import os

import httpx

from gateway.config import settings
from gateway.metrics import HEALTHY_MULTIMODAL_NODES, HEALTHY_TEXT_NODES

logger = logging.getLogger(__name__)

PROBE_INTERVAL = 15   # seconds between full rounds
PROBE_TIMEOUT = 3.0   # per-node HTTP timeout

# Module-level sets — updated in-place by the background loop.
# Both pools start optimistic (all nodes healthy) so the first real requests
# aren't blocked waiting for the first probe cycle to complete.
healthy_text_nodes: set[str] = set()
healthy_multimodal_nodes: set[str] = set()

# Bypass corporate proxy for internal 10.x health probes.
os.environ.setdefault("NO_PROXY", settings.no_proxy)
os.environ.setdefault("no_proxy", settings.no_proxy)


def _parse_ips(csv: str) -> list[str]:
    return [ip.strip() for ip in csv.split(",") if ip.strip()]


async def _probe(ip: str, port: int) -> bool:
    try:
        async with httpx.AsyncClient(trust_env=True) as client:
            r = await client.get(f"http://{ip}:{port}/health", timeout=PROBE_TIMEOUT)
            return r.status_code == 200
    except Exception:
        return False


async def _probe_pool(ips: list[str], port: int, healthy: set[str]) -> None:
    results = await asyncio.gather(*[_probe(ip, port) for ip in ips])
    for ip, ok in zip(ips, results):
        was_healthy = ip in healthy
        if ok:
            if not was_healthy:
                logger.info("health_monitor: %s recovered", ip)
            healthy.add(ip)
        else:
            if was_healthy:
                logger.warning("health_monitor: %s marked unhealthy", ip)
            healthy.discard(ip)


async def health_probe_loop() -> None:
    text_ips = _parse_ips(settings.text_node_ips)
    multimodal_ips = _parse_ips(settings.multimodal_node_ips)

    healthy_text_nodes.update(text_ips)
    healthy_multimodal_nodes.update(multimodal_ips)

    logger.info(
        "health_monitor: starting — %d text nodes, %d multimodal nodes, probe every %ds",
        len(text_ips), len(multimodal_ips), PROBE_INTERVAL,
    )
    while True:
        await _probe_pool(text_ips, settings.text_llama_port, healthy_text_nodes)
        await _probe_pool(multimodal_ips, settings.multimodal_llama_port, healthy_multimodal_nodes)
        HEALTHY_TEXT_NODES.set(len(healthy_text_nodes))
        HEALTHY_MULTIMODAL_NODES.set(len(healthy_multimodal_nodes))
        await asyncio.sleep(PROBE_INTERVAL)
