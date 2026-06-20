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

Retries are capped at `_MAX_RETRIES = 5` (6 attempts total). Two backoff
schedules exist:

- `_FAST_REROUTE_BACKOFFS_SECONDS = [0.5, 1.5]` — used when we know a
  different, healthy node exists to send the retry to. Unaffected by the cap
  above except for the one extra attempt, which simply clamps to `1.5s` like
  any attempt past the schedule's length — known-healthy reroutes never pay
  the longer wait below.
- `_POOL_EXHAUSTED_BACKOFFS_SECONDS = [1.0, 15.0, 30.0, 60.0, 120.0]` — used
  when we have no alternate node to reroute to and just have to wait out a
  recovery. The `120.0` step was added after multimodal nodes (`.65`/`.67`)
  were observed taking 100s+ to recover — occasionally crossing the
  watchdog's own 120s restart-wait into a second cycle — pushing the total
  wait budget from 106s to 226s.

| Case | Trigger | Meaning | Handling |
|---|---|---|---|
| **A — node failure, alternate exists** | HTTP error response, `node_ip` present, a different healthy node is known | A specific backend failed, but we know who else is healthy | Evict the failed node (`_reroute_after_node_failure`), point the retry at a different node's proxy, fast backoff (`_FAST_REROUTE_BACKOFFS_SECONDS`) — no recovery wait needed, we're not going back to it |
| **B — pool exhausted / reroute defeated** | HTTP error response, `node_ip` present, no alternate node left — *or* a rerouted retry still landed back on the same bad node | Either every node is excluded, or Ray Serve's cluster-wide deployment router ignored our "different node" URL anyway (see note below) | Long backoff (`_POOL_EXHAUSTED_BACKOFFS_SECONDS`) to wait out the node's actual recovery window (~56s observed) rather than hammering a node that isn't back yet |
| **C — queue backpressure** | HTTP 503, no `node_ip` | Ray Serve rejected the request before any replica ran (`max_queued_requests` exceeded) — real demand exceeds real capacity, nothing "failed" | **No retry.** A guessed wait doesn't fix a full queue — fail fast |
| **D — actor died** | HTTP 500, no `node_ip` | The Ray actor itself died (e.g. controller force-killed a replica that failed its health check) before our worker code ran far enough to stamp a `node_ip` | Fast backoff, same tier as A — Ray's controller already evicted the dead replica, so the same URL lands on a different, already-healthy one |
| **E — connection failure** | `httpx.ConnectError` or `httpx.ConnectTimeout` — no HTTP response at all | TCP connection to the target node's Ray Serve proxy port itself failed (proxy mid-crash-restart, raylet down entirely) or never completed within `connect_timeout_seconds` (proxy too backed up to accept new connections — observed on the multimodal pool's central chokepoint during sustained node instability, 2026-06-19/20) | Extract the host from the URL we just tried (no response body exists to read a `node_ip` from), evict it the same way as case A, reroute with fast backoff if an alternate exists, else the pool-exhausted backoff |

**Observability:** every reroute decision (`_reroute_after_node_failure`, shared
by cases A, B, and E) now logs to `gateway/ray_client.py`'s logger:
`INFO` — `"reroute: <failed_ip> failed, rerouting <text|multimodal> request to
<new_ip> (excluded=[...])"` on success, `WARNING` — `"reroute: <failed_ip>
failed, no alternate <text|multimodal> node available (excluded=[...]) — pool
exhausted"` when there's nothing left to reroute to. Before this, a
successfully-retried request was invisible in `request_logs` (which only
records the *final* outcome) — `grep "reroute:" ` against `docker logs
llm-gateway` is now the way to measure how often each case actually fires,
rather than inferring it from watchdog timing after the fact.

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
(`httpx.ReadTimeout` / other `httpx.TimeoutException` subclasses besides
`ConnectTimeout`, which is classified under case E instead), `submit_inference`
retries once (`_TIMEOUT_MAX_RETRIES = 1`) through the central proxy.
**Non-streaming only** — a streaming response may have already sent bytes to
the client by the time it times out, so `stream_inference` cannot safely
retry it. `ConnectTimeout` used to land here too (since it's technically a
`TimeoutException` subclass), but a connection that never completed isn't a
slow read — it gets case E's full tiered backoff and reroute logic instead.

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

## Worst case: what the caller actually sees

If every retry is exhausted, the framework doesn't swallow the failure — it
gives up and returns an error to the caller. Excluding `429` (which isn't a
backend failure, see above), the caller should expect one of exactly four
outcomes, never anything unstructured:

| Status | When | Body |
|---|---|---|
| **503** | Case E exhausted (`_MAX_RETRIES` `ConnectError`/`ConnectTimeout` attempts used up) | `{"error": "Unable to reach inference backend: <exc>"}` — raised explicitly in `submit_inference`/`stream_inference` |
| **503** | Case C (queue backpressure) — immediate, no retry spent | Worker's `"Inference server unavailable..."` body passed through as-is |
| **503 / 500 / 504** | Case A/B/D exhausted — `_MAX_RETRIES` used up while still landing on a failing node | The *last* attempt's real status code and body passed through verbatim (`raise HTTPException(status_code=response.status_code, detail=detail)`) — whatever the worker last reported (`worker/ray_worker.py`'s table below decides which of 500/503/504 that is) |
| **500** | Any exception not classified into A–E (the catch-all in `gateway/routers/chat.py`) | Generic `"Inference request failed"`, `node_ip="unknown"` — by design a shrinking category as more cases get classified by name (case E was the last one added this way) |

In other words: in the worst case a caller gets a `503` most of the time
(every explicit-give-up path above defaults to it), occasionally a `500` or
`504` if that's literally what the last attempt's real backend error was, and
the response body always tells you which: either a structured
`{"error": ..., "node_ip": ...}` (worker-classified) or a plain `{"error":
"Unable to reach..."}` / `"Inference request failed"` (gateway gave up).
There is no silent timeout from the caller's point of view — `_MAX_RETRIES`
plus the backoff schedules bound the worst case to **~226s** (case B/E
pool-exhausted path) before *something* comes back.

**Infra root causes feeding into this (out of scope for this doc, see
`scripts/llama_watchdog.sh`):** this session found that the actual frequency
of cases B/E was inflated by an infra-level bug, not just transient node
flakiness — `restart_raylet()` was calling `ray start` without first reaping
the previous (crashed but not exited) raylet, which on five nodes
(`.65`/`.67`/`.64`/`.60`/`.53`) had silently accumulated into 2–12 duplicate
raylet registrations per node, two of which (`.64`, `.60`) had Ray Serve
double-scheduling a live replica per duplicate — i.e. two `MultimodalWorker`/
`TextWorker` actors contending for one physical GPU, which is a very plausible
direct cause of some of the worker-side crashes feeding case A/B/D in the
first place. `ray stop --force` was added before `ray start` to fix this at
the source; the duplicates were manually cleaned up (cluster back to the
correct 13 active nodes). This won't reduce case A/D to zero — real node
failures will still happen — but it should reduce how often the *same* nodes
flap from self-inflicted resource contention.

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
- Excluding 429, the worst case a caller can see is always one of **503
  (most common), 500, or 504** — never an unbounded hang — within a hard
  ceiling of ~226s. The `reroute:` log lines in `gateway/ray_client.py` make
  it possible to measure how often each case actually fires, instead of
  inferring it after the fact.
