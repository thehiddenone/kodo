# LLM Gateway

> Status: implemented (2026-06-21). Source:
> [llms/_gateway.py](../src/kodo/llms/_gateway.py). Tests:
> [test/test_llm_gateway.py](../test/test_llm_gateway.py).

With a singleton server driving **many sessions concurrently**, LLM access must
be coordinated: the single local `llama-server` can serve only one request at a
time, and cloud providers rate-limit. The **`LLMGateway`** is the one process-
wide component every session's engine schedules through. LLM plugins stay
stateless one-shot facades — **all** queue / concurrency / throttle policy lives
in the gateway.

Not every caller is a session: the `llm.complete` handler (WS_PROTOCOL.md
§7.6b, built for `kodo.validator`) schedules a session-less one-shot local
completion through the same local feed, with the requesting *connection* as
its `llm.waiting` sink — so it serializes with session dispatches instead of
racing llama-server.

## API

```python
gateway.stream_query(*, routing: LLMRouting, plugin: LLMPlugin, sink, **stream_kwargs)
```

Mimics `LLMPlugin.stream_query` and yields its events. `routing` selects the
feed; `sink` is the originating session's channel (the gateway emits its
`llm.waiting` events through it). `stream_kwargs` are forwarded verbatim to the
plugin. The engine resolves `(plugin, model_id, LLMRouting)` from settings and
keeps the API key per session — the gateway never touches keys.

```python
LLMRouting(residence="local")                 # the one shared local feed
LLMRouting(residence="cloud", vendor="anthropic")  # per-vendor cloud feed
```

## Feeds

One `_Feed` per key:

- **`local`** — `max_slots = 1`. **All** local plugins (any model) share this one
  serial gate, so the local server is never asked to serve two requests at once.
- **`cloud:<vendor>`** — `max_slots = cloud_concurrency()` (read fresh from
  `~/.kodo/etc/settings.json` `cloud_concurrency`, default **2**, so the limit is
  live-configurable). One feed per vendor → different vendors run in parallel.

A feed is a **delay-aware FIFO admission controller**: a request first sleeps out
its `ready_at` delay, then competes for a slot strictly in arrival order (an
`asyncio.Condition`). `now`/`sleep` are injectable for deterministic tests.

## Waiting indicator

When a request cannot be admitted immediately the gateway emits
`llm.waiting {waiting:true, reason:"queued"}` to the session, and `{waiting:false}`
once admitted. The webview shows "LLM is busy, waiting …".

## 429 throttling (cloud)

Rate-limit policy is **vendor-stateful**, held in the feed:

- The plugin surfaces an HTTP 429 as the provider-agnostic `RateLimited`
  (`anthropic/_retry.py` no longer treats it as a terminal error). It captures
  a `Retry-After` header when present, but the header is **never** used
  verbatim as the delay — see below.
- On 429 (raised before any event is yielded), the gateway re-queues the request
  with `ready_at = now + delay` where `delay = max(retry_after, current_backoff)`,
  plus jitter (see below), and **doubles** `current_backoff` (`min(*2, 3600s)`);
  the base is **60 s**, so consecutive throttles wait 1, 2, 4, 8 … minutes. Any
  **successful** request resets the backoff to the base.
- **`Retry-After` is a floor, not a substitute.** A vendor's advertised delay is
  only ever used when it *exceeds* the currently computed backoff (e.g. a real
  quota-reset window worth respecting); a vendor that keeps sending a short,
  fixed `Retry-After` on every 429 — as Moonshot's Kimi API does
  (`Retry-After: 1` on every throttled response, a per-request quota cooldown
  hint rather than a real backoff signal) — cannot pin the delay at that value
  and defeat the exponential growth. This was a real bug (2026-08-16): the
  gateway used to honor `Retry-After` verbatim whenever present, so a vendor
  advertising a constant 1s cooldown produced an unbounded retry loop that
  hammered the API roughly once a second instead of backing off.
- A small positive **jitter** (up to +25% of the computed delay, randomized) is
  added on top of the final delay so concurrent retries against the same
  vendor don't resynchronize. `LLMGateway`/`_Feed` take an injectable `jitter:
  Callable[[], float]` seam (defaults to `random.random`) for deterministic
  tests.
- It emits `llm.waiting {reason:"throttled", retry_in_seconds}`; the extension
  shows an auto-dismissing notice and the webview shows
  "Getting throttled, waiting for X minutes".
- A 429 that arrives **mid-stream** (after events were yielded) is surfaced as a
  normal error rather than restarting a partial stream (documented limitation).

**The SDK's own built-in retry is disabled** (`max_retries=0` on every vendor's
`openai.AsyncOpenAI`/`anthropic.AsyncAnthropic` client construction). Both SDKs
default to `max_retries=2`, which — left enabled — silently retries 429s and
5xx/timeout errors *inside* the SDK first, on its own short, un-jittered
schedule (and, for 429s, driven by the same misleading vendor `Retry-After`),
before either `kodo.llms._provider_retry` or the gateway ever see the error.
This module and the gateway are the sole owners of retry/backoff policy for
every cloud vendor; a new vendor plugin must construct its SDK client with
`max_retries=0` too.

## Cancellation & release

A cancelled call releases its slot / drops its queue position in `finally`, so a
following request proceeds. Slots are always released on success and on error.
