# Local Inference — Robustness of `llama-server` Tool Calling

> How Kōdo hardens local (llama.cpp) inference against the format slips a
> local model makes that a hosted API never would: the `llama-server` launch
> flags, salvaging a tool call a model emitted as plain text, and stripping
> stray `<think>` tags from reasoning.

Companion to [LLM_REGISTRY.md](LLM_REGISTRY.md) (which local models exist and
how they are configured), [LLM_GATEWAY.md](LLM_GATEWAY.md) (request routing),
and [SECURITY.md](SECURITY.md) §9 (the user-facing confirmation for a recovered
call). All code lives under `kodo/llms/llamacpp/`.

---

## 1. The problem

A hosted API (Anthropic) returns tool calls as first-class structured objects.
A local model does not: `llama-server` recognises a tool call only by parsing
the model's raw token stream, and a local model's output format can *slip*.

The motivating failure: **gpt-oss** uses OpenAI's "harmony" format, where a
tool call must go in a `commentary` channel with a `recipient` header naming the
function. On large, escape-heavy arguments (e.g. a whole-file rewrite with
deeply nested quotes) gpt-oss sometimes emits the call's JSON in the **`final`**
(user-facing content) channel instead. `llama-server` then returns that JSON as
ordinary `message.content` with **no** `tool_calls` and, because the recipient
header was never emitted, **no function name**. The turn ends `end_turn` with
zero tool calls, so — without the guards below — the engine treats a failed
tool call as a finished answer: it persists the JSON blob as assistant text and
goes idle. A failed tool call is otherwise indistinguishable from a completed
turn.

Kōdo addresses this on two fronts: reduce the frequency (§2) and catch it
regardless of frequency (§3).

## 2. Launch flags: `--jinja` + `--reasoning-format auto`

`LlamaServer.__build_command` (`_llama_server.py`) launches **every** local
model's `llama-server` with:

```
--jinja --reasoning-format auto
```

- `--jinja` makes `llama-server` use the GGUF's **embedded chat template** and
  enables template-driven, grammar-constrained ("lazy grammar") tool-call
  parsing. Without it the parser is best-effort recognition only, with nothing
  forcing a model back into a valid tool-call structure once its format drifts.
- `--reasoning-format auto` lets the reasoning channel be parsed per the
  model's own convention (for gpt-oss, reasoning arrives as `reasoning_content`
  rather than inline `<think>` tags).

**Scope decision:** applied to all local models, not just gpt-oss. Modern
Qwen/Gemma/gpt-oss GGUFs all ship a valid embedded template, and the salvage
path (§3) covers any residual slip regardless of model. `--jinja` *reduces* the
frequency of a wrong-channel slip but does not eliminate it — it constrains
tool-call **syntax** once a call triggers; it does not force the model to
choose the tool-call channel in the first place. So §3 is still required.

## 3. Salvaging a tool call emitted as plain text

`LlamaPlugin.__raw_stream` (`_llama.py`) watches for the slip and recovers it.

**Buffering.** The content channel is normally streamed live token-by-token.
But if the first non-whitespace character is `{`, the content is *withheld*
(buffered, not streamed) because it might be a whole tool call — streaming it
live would flash the raw JSON into the feed. Ordinary prose (anything not
starting with `{`) streams live as before; the only cost is a slight delay on
the rare turn whose answer genuinely begins with `{`.

**Decision (end of stream).** If the model made no structured tool call and the
buffered content parses as a JSON **object**:

- `_match_salvage_tools` finds the tools whose input schema plausibly owns the
  arguments — the function name was lost, so the tool is inferred from the
  argument **shape**: every provided key must be a declared property of the
  tool, and every required property must be present.
- **Exactly one** match → synthesise `ToolCallEvent(recovered=True)` with a
  fresh `recovered_<uuid>` id; the JSON is **not** shown as text. The engine
  routes `recovered=True` to the security gate, which forces a user
  confirmation outside Autonomous mode (see [SECURITY.md](SECURITY.md) §9). In
  Autonomous mode it just runs.
- **Zero or several** matches → cannot recover unambiguously: raise
  `MalformedToolCallError`. The worker's generic handler turns it into a
  recoverable `error_notice` and resets the phase to `awaiting_user`; the raw
  JSON is discarded (not shown as an answer) and the model is expected to
  retry.

If the buffered content merely *began* with `{` but was not a valid tool-call
object (ordinary prose, or a leading-`{` preamble before a real structured
call), it is released as normal text.

## 4. Stripping stray `<think>` tags from reasoning

gpt-oss reasoning frequently carries literal `<think>…</think>` tags —
sometimes many in a row, sometimes nested — which are noise once the text is
already shown in a thinking block. `_ThinkTagStripper` (`_llama.py`) removes the
tag **tokens** of **balanced** pairs while keeping the inner text; an
**unmatched** tag is emitted verbatim so nothing is silently swallowed.

- It streams safely: a tag split across two chunks is held until it resolves,
  and an unclosed region is released verbatim on `flush`.
- It is applied to thinking/reasoning text only — never to user-facing output —
  via two independent instances: one for the model's `reasoning_content`
  channel, one for any `<think>` the `ThinkingStreamParser` lifts out of the
  content channel (a `<think>` opened in one channel never closes in the other).
- "Balanced pairs only" is deliberate (a lone `<think>` is left intact rather
  than guessed at). Depth counting handles nesting: an outer balanced region
  has all of its tags stripped.

## 5. Why these are local-only

All three behaviours live in `kodo/llms/llamacpp/` and never touch the
Anthropic plugin: a hosted API returns structured tool calls and signed
thinking blocks, so none of these slips occur there. `ToolCallEvent.recovered`
defaults to `False`, so the cloud path and the crash-resume path are unaffected.
