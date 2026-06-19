# Error Handling

How the gateway classifies and reacts to failures between itself, Ray Serve,
and the llama.cpp backends. This only covers `/v1/chat/completions` request
handling (`gateway/ray_client.py`, `gateway/routers/chat.py`,
`gateway/rate_limiter.py`) — not infra-level recovery like the llama-server
watchdog (see `scripts/llama_watchdog.sh`).

Every worker (`worker/ray_worker.py`) stamps its own `node_ip` on every error
body it produces. That single fact is what lets the gateway tell these cases
apart instead of using one flat retry policy for everything: presence/absence
of `node_ip`, plus the HTTP status code (or the total absence of an HTTP
response), determines which bucket a failure falls into.

## Retryable cases (`gateway/ray_client.py`)

Retries are capped at `_MAX_RETRIES = 4` (5 attempts total). Two backoff
schedules exist:

- `_FAST_REROUTE_BACKOFFS_SECONDS = [0.5, 1.5]` — used when we know a
  different, healthy node exists to send the retry to.
- `_POOL_EXHAUSTED_BACKOFFS_SECONDS = [1.0, 15.0, 30.0, 60.0]` — used when we
  have no alternate node to reroute to and just have to wait out a recovery.

| Case | Trigger | Meaning | Handling |
|---|---|---|---|
| **A — node failure, alternate exists** | HTTP error response, `node_ip` present, a different healthy node is known | A specific backend failed, but we know who else is healthy | Evict the failed node (`_reroute_after_node_failure`), point the retry at a different node's proxy, fast backoff (`_FAST_REROUTE_BACKOFFS_SECONDS`) — no recovery wait needed, we're not going back to it |
| **B — pool exhausted / reroute defeated** | HTTP error response, `node_ip` present, no alternate node left — *or* a rerouted retry still landed back on the same bad node | Either every node is excluded, or Ray Serve's cluster-wide deployment router ignored our "different node" URL anyway (see note below) | Long backoff (`_POOL_EXHAUSTED_BACKOFFS_SECONDS`) to wait out the node's actual recovery window (~56s observed) rather than hammering a node that isn't back yet |
| **C — queue backpressure** | HTTP 503, no `node_ip` | Ray Serve rejected the request before any replica ran (`max_queued_requests` exceeded) — real demand exceeds real capacity, nothing "failed" | **No retry.** A guessed wait doesn't fix a full queue — fail fast |
| **D — actor died** | HTTP 500, no `node_ip` | The Ray actor itself died (e.g. controller force-killed a replica that failed its health check) before our worker code ran far enough to stamp a `node_ip` | Fast backoff, same tier as A — Ray's controller already evicted the dead replica, so the same URL lands on a different, already-healthy one |
| **E — connection failure** | `httpx.ConnectError` — no HTTP response at all | TCP connection to the target node's Ray Serve proxy port itself failed (proxy mid-crash-restart, or the node's raylet down entirely) | Extract the host from the URL we just tried (no response body exists to read a `node_ip` from), evict it the same way as case A, reroute with fast backoff if an alternate exists, else the pool-exhausted backoff |

**Why case B exists at all despite explicit node exclusion:** Ray Serve's
HTTP proxy runs in `EveryNode` mode — hitting a specific node's proxy does
**not** guarantee that node's replica serves the request. Ray's
deployment-level router (power-of-two-choices) picks a replica cluster-wide
regardless of which node's proxy accepted the connection. Worse, a replica
that fails every request instantly never accumulates queue depth, so it
*looks* like the most available replica to that scheduler — increasing, not
decreasing, the odds it gets picked again while broken. The gateway can't
out-route that bias by URL choice alone; case B's longer backoff is how we
out-wait it instead.

**Gateway-side read timeout (separate, not part of A–E):** if the gateway's
own HTTP call to the backend exceeds `request_timeout_seconds`
(`httpx.ReadTimeout` / `httpx.TimeoutException`), `submit_inference` retries
once (`_TIMEOUT_MAX_RETRIES = 1`) through the central proxy. **Non-streaming
only** — a streaming response may have already sent bytes to the client by
the time it times out, so `stream_inference` cannot safely retry it.

## Non-retryable, request-rejected cases

| Source | Status | Trigger | Handling |
|---|---|---|---|
| `gateway/rate_limiter.py` | 429 | Per-user sliding-window limit exceeded (Redis-backed) | Rejected immediately, before reaching Ray at all — `"Rate limit exceeded: {limit} requests per {window}s"` |
| `gateway/routers/chat.py` (generic handler) | 500 | Any exception not otherwise caught (should now be rare — case E was the main gap here) | Logged with `node_ip=unknown`, generic `"Inference request failed"` returned to caller |

## Worker-side error shaping (`worker/ray_worker.py`)

Each `TextWorker`/`MultimodalWorker` replica's own call to its local
llama-server is wrapped so the *type* of failure determines the status code
and body it hands back upstream — this is what makes cases A–D in the
gateway's table possible (it's where `node_ip` gets stamped):

| llama-server-side exception | Status returned upstream | Meaning |
|---|---|---|
| `httpx.HTTPStatusError` | passthrough (llama-server's own status) | llama-server itself returned an error |
| `httpx.RemoteProtocolError` | 503 | Connection dropped mid-generation — server likely crashed |
| `httpx.ConnectError` | 503 | llama-server unreachable from its own node — starting up or restarting |
| `httpx.ReadTimeout` / `httpx.TimeoutException` | 504 | llama-server stuck/overloaded on this node |

All four include `node_ip` in the body, which is what feeds case A/B/D
routing one layer up in the gateway.

## Summary

- The gateway tells failures apart by **whether `node_ip` is present** and
  **what status code (if any) came back**, not by guessing.
- **Known-bad node, alternate available → fast reroute (A).** **Known-bad
  node, no alternate or reroute defeated by Ray's cluster-wide routing →
  long wait (B).** **Queue full, nothing broken → don't retry (C).** **Actor
  died, already evicted by Ray → fast retry (D).** **Connection never
  established at all → extract the host from the URL, evict it, reroute like
  A/B (E).**
- Rate limiting (429) and unclassified exceptions (generic 500) sit outside
  this retry framework entirely — by design for 429 (it's not a backend
  failure), and as a last-resort catch-all for the 500 (the goal over time is
  to shrink how often this path is hit by classifying more cases like E).
