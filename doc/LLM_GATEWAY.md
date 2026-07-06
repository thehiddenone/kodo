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
  (`anthropic/_retry.py` no longer treats it as a terminal error). It honors a
  `Retry-After` header when present.
- On 429 (raised before any event is yielded), the gateway re-queues the request
  with `ready_at = now + current_backoff` and **doubles** `current_backoff`
  (`min(*2, 3600s)`); the base is **60 s**, so consecutive throttles wait
  1, 2, 4, 8 … minutes. Any **successful** request resets the backoff to the base.
- It emits `llm.waiting {reason:"throttled", retry_in_seconds}`; the extension
  shows an auto-dismissing notice and the webview shows
  "Getting throttled, waiting for X minutes".
- A 429 that arrives **mid-stream** (after events were yielded) is surfaced as a
  normal error rather than restarting a partial stream (documented limitation).

## Cancellation & release

A cancelled call releases its slot / drops its queue position in `finally`, so a
following request proceeds. Slots are always released on success and on error.
