# Sampling Parameters — `llama-server` Request-Level and CLI-Level Knobs

> What every `llama-server` sampling parameter does, what it does to generated
> text, the range of values worth trying, and how Kōdo exposes the two layers:
> **CLI-level** parameters, fixed at launch by a flavor, and **request-level**
> parameters, tuned live from the chat footer.

Companion to [LLM_REGISTRY.md](LLM_REGISTRY.md) (§4.6 flavors — where CLI-level
args, including the ones a flavor's sampling-defaults form writes, are
declared), [LOCAL_INFERENCE.md](LOCAL_INFERENCE.md) (the launch flags Kōdo
forces regardless of flavor), and [SESSIONS.md](SESSIONS.md) (session-scoped
state, where the per-quant overrides live).

---

## 1. The two layers, and why both exist

`llama-server` accepts the same sampling knobs twice:

- **CLI-level**, e.g. `llama-server --temp 0.6 --top-p 0.9 …`. Fixed for the
  life of the process. Changing one means restarting the server.
- **Request-level**, e.g. `{"temperature": 0.6, "top_p": 0.9, …}` in the
  `POST /v1/chat/completions` body. Applies to that one request.

The relationship is not "two independent settings" — it is a **default and an
override**. For each request, `llama-server` starts from the launch-time
sampling params and then overwrites individual fields present in the request
body (`params_from_json_cmpl` reads each key as
`json_value(data, "<key>", defaults.sampling.<field>)`, where `defaults` is the
server's own launch config). So:

> **A request-level field's default is whatever the server was launched with.**
> Omit `temperature` from a request against a server started with
> `--temp 0.6`, and that request runs at 0.6 — not at llama.cpp's built-in
> 0.8.

Two consequences that matter for Kōdo:

1. **Omitting a field is meaningful.** It is not the same as sending the
   library default. This is why every sampling field in Kōdo's data model is
   optional (`None`) and is **dropped from the request body entirely** when
   unset, rather than being sent as some stand-in value. Sending
   `"temperature": 0.8` because the user left the box empty would silently
   defeat a flavor's `--temp 0.6`.
2. **A knob set in both places is not a conflict the server resolves oddly** —
   the request-level value simply wins, every time, for as long as it is sent.
   The CLI value still governs any client that does *not* send the field
   (another app pointed at the same server, or Kōdo itself once the user
   clears the override).

The "default" column in every table below therefore means *llama.cpp's
built-in value, used when the server was launched with no corresponding flag* —
not "what Kōdo sends".

### 1a. Unknown parameters are inert, not fatal

`params_from_json_cmpl` looks up keys it knows and ignores everything else. A
parameter introduced in a newer llama.cpp than the installed build is silently
ignored rather than rejected, so a flavor or session override naming one is
harmless — it just has no effect until the user updates their llama.cpp. There
is no error and no warning; if a knob appears to do nothing, an out-of-date
`llama-server` is the first thing to check
(LLM_REGISTRY.md §4.2 covers the binary override, and the sidebar surfaces the
installed version).

---

## 2. The sampling chain

Sampling is a **pipeline**, not a set of independent dials. Each step receives
the token distribution left by the previous one. The default order is:

```
penalties → dry → top_n_sigma → top_k → typ_p → top_p → min_p → xtc → temperature
```

(`--samplers`, semicolon-separated on the CLI; a JSON array of the same names
on a request.) Order is why some combinations behave unintuitively:

- **Temperature is last.** The truncation samplers (`top_k`, `top_p`, `min_p`,
  …) cut the candidate set using the *raw* probabilities; temperature then
  reshapes what survives. So raising temperature cannot resurrect a token
  `min_p` already discarded.
- **Truncation samplers compose by intersection.** Running `top_k 40`,
  `top_p 0.95` and `min_p 0.05` together means the surviving set is whichever
  is strictest at that position. Stacking three is not three times the effect —
  usually one of them is doing all the work and the others never bind.
- **`min_keep` is the floor.** No truncation sampler may cut the candidate set
  below `min_keep` tokens, whatever its own threshold says.

**Practical advice:** pick *one* truncation sampler and neutralise the others,
rather than tuning three at once. `min_p` alone (with `top_k 0`, `top_p 1.0`)
is the common modern choice; `top_p` alone is the traditional one.

A sampler is disabled by setting it to its no-op value — listed per parameter
below, and collected in §8.

---

## 3. Temperature

### `temperature` (CLI `--temp`, `--temperature`)

Scales the logits before the final softmax, controlling how much probability
mass the model is willing to move away from its top choice.

| | |
|---|---|
| **Default** | `0.8` |
| **Range** | `0.0` – `2.0` in practice (`>2.0` accepted but degenerates) |
| **Disabled** | `1.0` (leaves the distribution untouched) |

- `0.0` — **greedy**. Always the single highest-probability token. Every
  truncation sampler upstream becomes irrelevant. Fully deterministic given the
  same prompt and the same server (`seed` stops mattering).
- `0.1` – `0.3` — near-deterministic. The right region for structured output,
  tool-call arguments, code edits, and anything that must parse.
- `0.6` – `0.8` — general-purpose. Most instruct models are tuned and evaluated
  around here.
- `1.0` – `1.3` — noticeably creative; also noticeably more likely to break a
  required format or hallucinate an API that does not exist.
- `> 1.5` — usually incoherent unless paired with an aggressive truncation
  sampler (`min_p 0.05`+) that removes the tail temperature would otherwise
  amplify.

**For agentic/tool-calling work** (Kōdo's whole workload) low is better. A
temperature that produces charming prose also produces a JSON argument blob
with a stray trailing comma. `0.0`–`0.3` is the sane band; the model-specific
recommendation from the GGUF's publisher is a better starting point than any
general rule.

### `dynatemp_range` (CLI `--dynatemp-range`), `dynatemp_exponent` (CLI `--dynatemp-exp`)

**Dynamic temperature**: instead of one fixed value, temperature varies per
token between `temperature - dynatemp_range` and `temperature + dynatemp_range`,
chosen from the entropy of the current distribution. Where the model is
confident (low entropy) it samples cold; where it is genuinely uncertain (high
entropy) it samples hot.

| | |
|---|---|
| **Defaults** | `dynatemp_range 0.0`, `dynatemp_exponent 1.0` |
| **Range** | `range` `0.0` – `~1.0`; `exponent` `0.0` – `~2.0` |
| **Disabled** | `dynatemp_range 0.0` |

`temperature 0.8` + `dynatemp_range 0.4` sweeps 0.4–1.2. The `exponent` biases
where in that band the mapping spends its time: `>1.0` pushes toward the cold
end, `<1.0` toward the hot end.

Worth trying for creative writing. For structured/agentic output it mostly adds
variance in exactly the places (uncertain moments) where determinism is most
valuable — leave it off.

---

## 4. Truncation samplers

These remove tokens from consideration before temperature is applied.

### `top_k` (CLI `--top-k`)

Keep only the `k` most probable tokens.

| | |
|---|---|
| **Default** | `40` |
| **Range** | `1` – `100` typical; up to vocabulary size |
| **Disabled** | `0` |

`1` is equivalent to greedy decoding. `20`–`50` is the conventional band. Its
weakness is being **absolute**: it keeps 40 candidates whether the model is
completely certain (where 39 of them are junk) or genuinely torn (where the
40th was a fine choice). That is what `top_p`/`min_p` fix, and why `top_k` is
increasingly used only as a cheap safety cap alongside them.

### `top_p` (CLI `--top-p`) — nucleus sampling

Keep the smallest set of tokens whose cumulative probability reaches `p`.

| | |
|---|---|
| **Default** | `0.95` |
| **Range** | `0.5` – `1.0` |
| **Disabled** | `1.0` |

Adaptive where `top_k` is not: a confident position keeps 1–2 tokens, an
uncertain one keeps many. `0.9`–`0.95` is standard. Below `~0.8` output gets
noticeably repetitive and safe; `0.5` is very tight.

Its weakness is the mirror image of `top_k`'s: on a flat distribution the
cumulative sum takes a long time to reach `p`, so it admits a long tail of
individually-implausible tokens.

### `min_p` (CLI `--min-p`)

Keep tokens whose probability is at least `min_p × P(most likely token)`.

| | |
|---|---|
| **Default** | `0.05` |
| **Range** | `0.01` – `0.2` |
| **Disabled** | `0.0` |

The modern default choice, because the threshold is **relative to the model's
own confidence** at that position. If the top token is at 0.9, `min_p 0.05`
admits only tokens above 0.045 — near-greedy. If the top token is at 0.1
(genuine uncertainty), the bar drops to 0.005 and many alternatives stay in
play. This makes it far more robust at high temperature than `top_p`.

`0.05` is a good general value; `0.1` is tighter; `0.02` is loose. A common
modern configuration is `min_p 0.05` with `top_k 0` and `top_p 1.0`, letting
`min_p` do all the truncation.

### `typical_p` (CLI `--typical`, `--typical-p`) — locally typical sampling

Keep tokens whose surprisal is closest to the distribution's own entropy —
i.e. tokens that are *typically* surprising for this position, discarding both
the boringly-obvious and the wildly-improbable.

| | |
|---|---|
| **Default** | `1.0` |
| **Range** | `0.2` – `1.0` |
| **Disabled** | `1.0` |

`0.9`–`0.95` reduces the "safe and dull" failure mode without opening the tail.
Rarely used; it interacts confusingly with `top_p`/`min_p`, so use it *instead
of* them, not alongside.

### `top_n_sigma` (CLI `--top-nsigma`, `--top-n-sigma`)

Keep tokens whose logit is within `n` standard deviations of the maximum logit.

| | |
|---|---|
| **Default** | `-1.0` |
| **Range** | `0.5` – `2.0` when enabled |
| **Disabled** | `-1.0` (any negative value) |

Operates on **logits** rather than post-softmax probabilities, which makes the
surviving candidate set largely **invariant to temperature** — the appealing
property is that you can raise temperature for variety without the usual
widening of the tail. `1.0` is the value the technique's authors suggest.
Useful if you want high-temperature output that still cannot pick nonsense.

### `min_keep`

A floor on how many candidates any truncation sampler may leave.

| | |
|---|---|
| **Default** | `0` |
| **Range** | `0` – `10` |
| **Disabled** | `0` |

Request-level only — there is **no CLI flag**. Insurance against an aggressive
threshold collapsing the candidate set to a single token at a position where
that was not intended. `1`–`5` if you use it at all.

---

## 5. Repetition control

### `repeat_penalty` (CLI `--repeat-penalty`) and `repeat_last_n` (CLI `--repeat-last-n`)

The classic penalty: divide the logit of any token that appeared in the last
`repeat_last_n` tokens by `repeat_penalty`.

| | |
|---|---|
| **Defaults** | `repeat_penalty 1.0`, `repeat_last_n 64` |
| **Range** | `penalty` `1.0` – `1.3`; `last_n` `0` – `2048`, or `-1` for the whole context |
| **Disabled** | `repeat_penalty 1.0`, or `repeat_last_n 0` |

- `1.0` — off.
- `1.05` – `1.15` — the usable band. `1.1` is the traditional value.
- `> 1.2` — actively harmful. The penalty is **blind to whether repetition is
  correct**: it penalises `}`, `return`, `import`, a variable name used twice,
  and every token of a file path the model is quoting back. On code and
  structured output this is a direct cause of syntax errors.

**For Kōdo's workload, leave this at 1.0.** Repetition penalties were designed
for open-ended prose from much weaker base models. Modern instruct models
rarely need one, and code is precisely the content where legitimate repetition
is constant. If a model genuinely loops, DRY (below) is the better instrument.

### `presence_penalty` (CLI `--presence-penalty`)

Flat additive penalty applied once to any token that has appeared at all.

| | |
|---|---|
| **Default** | `0.0` |
| **Range** | `-2.0` – `2.0` |
| **Disabled** | `0.0` |

`0.1`–`0.6` nudges toward introducing new vocabulary. Negative values encourage
staying on topic. Same blindness problem as `repeat_penalty` on structured
output, though the effect is gentler because it does not compound.

### `frequency_penalty` (CLI `--frequency-penalty`)

Additive penalty **proportional to how many times** a token has appeared.

| | |
|---|---|
| **Default** | `0.0` |
| **Range** | `-2.0` – `2.0` |
| **Disabled** | `0.0` |

`0.1`–`0.8` for prose. Because it scales with count, it is the more aggressive
of the two on code — a token like `self` or `const` accumulates penalty fast.
Leave at `0.0` for agentic use.

### The DRY family — `dry_multiplier`, `dry_base`, `dry_allowed_length`, `dry_penalty_last_n`, `dry_sequence_breakers`

**DRY** ("Don't Repeat Yourself") is a smarter repetition penalty: rather than
penalising individual tokens, it detects that the model is **replaying an
n-gram it has already emitted** and penalises continuing that replay,
exponentially in the length of the match.

| Parameter | Default | Range | Notes |
|---|---|---|---|
| `dry_multiplier` | `0.0` | `0.0` – `~5.0` | Master switch; `0.0` disables the whole family |
| `dry_base` | `1.75` | `1.0` – `4.0` | Exponential base — how fast the penalty grows with match length |
| `dry_allowed_length` | `2` | `1` – `20` | Repeat runs up to this length are free |
| `dry_penalty_last_n` | `-1` | `-1`, `0`, or a token count | Lookback window; `-1` = whole context, `0` = disabled |
| `dry_sequence_breakers` | `["\n", ":", "\"", "*"]` | list of strings | Tokens that reset match tracking |

Penalty for a repeat of length `L > allowed_length` grows as
`multiplier × base^(L − allowed_length)`. So a two-token echo costs nothing, a
ten-token echo is heavily suppressed, and a whole repeated paragraph is
effectively impossible.

`dry_multiplier 0.8`, `dry_base 1.75`, `dry_allowed_length 2` is the commonly
cited starting configuration.

**Why DRY is the right tool where `repeat_penalty` is the wrong one:** it
targets *verbatim looping* — the actual failure mode — rather than *token
reuse*. A model writing correct Python reuses `return` constantly but does not
replay a twelve-token sequence; a model stuck in a loop does exactly that.
Raising `dry_allowed_length` (to, say, `4`–`6`) gives further headroom for code
with legitimately repetitive structure (import blocks, long match arms).

Still worth being careful with on code: a large repeated literal (a base64
blob, a long table, a file being rewritten with few changes) *is* a long
verbatim repeat, and DRY will fight it. `dry_sequence_breakers` mitigates this
by resetting the match at structural boundaries — adding `"\t"`, `";"`, `","`,
`"}"` helps on source code.

### `adaptive_target` (CLI `--adaptive-target`), `adaptive_decay` (CLI `--adaptive-decay`)

**Adaptive-p**: continuously adjusts truncation aggressiveness to hold the
selected token's probability near a target, using an exponentially decaying
running estimate.

| | |
|---|---|
| **Defaults** | `adaptive_target -1.0`, `adaptive_decay 0.90` |
| **Range** | `target` `0.0` – `1.0` when enabled; `decay` `0.0` – `0.99` |
| **Disabled** | `adaptive_target` negative |

A recent addition — check that the installed llama.cpp build actually supports
it (§1a: it is silently ignored if not). Experimental; not a starting point.

---

## 6. Exotic samplers

### `xtc_probability`, `xtc_threshold` (CLI `--xtc-probability`, `--xtc-threshold`)

**XTC** ("Exclude Top Choices") inverts the usual idea: with probability
`xtc_probability`, it *removes* the most-likely tokens — every candidate above
`xtc_threshold` except the last one — deliberately forcing a less obvious
continuation.

| | |
|---|---|
| **Defaults** | `xtc_probability 0.0`, `xtc_threshold 0.1` |
| **Range** | `probability` `0.0` – `1.0`; `threshold` `0.0` – `0.5` |
| **Disabled** | `xtc_probability 0.0` (or `xtc_threshold > 0.5`) |

`xtc_probability 0.5`, `xtc_threshold 0.1` is the usual creative-writing
setting. It is very effective at killing clichéd phrasing and very effective at
killing correct code. **Never enable this for tool calling or code
generation** — the token XTC removes is frequently the only syntactically valid
one.

### `mirostat`, `mirostat_tau` (CLI `--mirostat-ent`), `mirostat_eta` (CLI `--mirostat-lr`)

**Mirostat** replaces the truncation samplers entirely with a feedback loop
that targets a constant output *perplexity*, adjusting its cutoff per token to
hold surprise at `tau`.

| | |
|---|---|
| **Defaults** | `mirostat 0`, `mirostat_tau 5.0`, `mirostat_eta 0.1` |
| **Range** | `mirostat` `0`/`1`/`2`; `tau` `2.0` – `8.0`; `eta` `0.01` – `1.0` |
| **Disabled** | `mirostat 0` |

`1` is the original algorithm, `2` the simplified/faster one. Lower `tau` means
more focused, higher means more surprising. `eta` is the feedback learning
rate.

**When mirostat is on, `top_k`/`top_p`/`min_p`/`typical_p` are bypassed** — it
is an alternative to them, not an addition. Mostly of historical interest now;
`min_p` achieves similar goals more predictably.

---

## 7. Determinism, constraints, and response shape

### `seed` (CLI `-s`, `--seed`)

RNG seed for sampling.

| | |
|---|---|
| **Default** | `-1` (random per request) |
| **Range** | any non-negative integer, or `-1` |

A fixed seed makes generation reproducible **only** if the prompt, the model,
the sampling parameters, the build, and the batching/slot conditions are all
identical — a busy server can reorder work in ways that change results. Useful
for debugging a specific bad generation; not a guarantee. `temperature 0.0` is
the stronger determinism lever.

### `ignore_eos` (CLI `--ignore-eos`)

Suppresses the end-of-stream token, forcing generation to continue until the
token cap.

| | |
|---|---|
| **Default** | `false` |

**Not exposed by Kōdo** (§9). In an agentic loop, enabling this means every
turn runs to `max_tokens` and no turn ever ends cleanly.

### `logit_bias` (CLI `-l`, `--logit-bias`)

Per-token additive bias, as `[[token_id, bias], …]`. Requires token IDs from
the specific model's tokenizer, which differ between models and are not
discoverable from Kōdo's UI. **Not exposed by Kōdo** (§9); reachable as a CLI
flag in a flavor if you know the IDs.

### `grammar` (CLI `--grammar`, `--grammar-file`) and `json_schema` (CLI `-j`, `--json-schema`)

Constrained decoding: restrict output to a GBNF grammar or a JSON schema.

**Reserved by Kōdo** (§9). `json_schema` is how structured LLM calls are
already implemented (`response_format`'s `json_object` form carries the schema,
`_llama.py`), and a user-supplied `grammar` would collide with the lazy
tool-call grammar `--jinja` installs (LOCAL_INFERENCE.md §2). Setting either by
hand would break tool calling.

### `n_probs`, `post_sampling_probs`

Ask the server to return per-token probabilities. Response-shape debugging
options, not sampling controls; Kōdo does not read those response fields, so
setting them only wastes bandwidth. Not exposed.

---

## 8. Quick reference

### 8a. No-op values

To neutralise a sampler rather than remove the field:

| Sampler | Neutral value |
|---|---|
| `temperature` | `1.0` |
| `top_k` | `0` |
| `top_p` | `1.0` |
| `min_p` | `0.0` |
| `typical_p` | `1.0` |
| `top_n_sigma` | `-1.0` |
| `repeat_penalty` | `1.0` |
| `presence_penalty` | `0.0` |
| `frequency_penalty` | `0.0` |
| `dry_multiplier` | `0.0` |
| `xtc_probability` | `0.0` |
| `mirostat` | `0` |
| `dynatemp_range` | `0.0` |
| `adaptive_target` | `-1.0` |
| `min_keep` | `0` |

Note the difference from *omitting* the field: omitting it inherits the
server's launch-time value (§1); sending the neutral value actively turns the
sampler off even if the flavor's CLI args enabled it.

### 8b. Request field ↔ CLI flag

| Request field | CLI flag |
|---|---|
| `temperature` | `--temp`, `--temperature` |
| `dynatemp_range` | `--dynatemp-range` |
| `dynatemp_exponent` | `--dynatemp-exp` |
| `top_k` | `--top-k` |
| `top_p` | `--top-p` |
| `min_p` | `--min-p` |
| `top_n_sigma` | `--top-nsigma`, `--top-n-sigma` |
| `typical_p` | `--typical`, `--typical-p` |
| `repeat_last_n` | `--repeat-last-n` |
| `repeat_penalty` | `--repeat-penalty` |
| `presence_penalty` | `--presence-penalty` |
| `frequency_penalty` | `--frequency-penalty` |
| `dry_multiplier` | `--dry-multiplier` |
| `dry_base` | `--dry-base` |
| `dry_allowed_length` | `--dry-allowed-length` |
| `dry_penalty_last_n` | `--dry-penalty-last-n` |
| `dry_sequence_breakers` | `--dry-sequence-breaker` (repeat per breaker) |
| `xtc_probability` | `--xtc-probability` |
| `xtc_threshold` | `--xtc-threshold` |
| `mirostat` | `--mirostat` |
| `mirostat_tau` | `--mirostat-ent` |
| `mirostat_eta` | `--mirostat-lr` |
| `adaptive_target` | `--adaptive-target` |
| `adaptive_decay` | `--adaptive-decay` |
| `seed` | `-s`, `--seed` |
| `samplers` | `--samplers` (JSON array vs. semicolon-separated string) |
| `min_keep` | *(none — request-level only)* |
| `ignore_eos` | `--ignore-eos` |
| `logit_bias` | `-l`, `--logit-bias` |
| `grammar` | `--grammar`, `--grammar-file` |
| `json_schema` | `-j`, `--json-schema` |

Note the three name mismatches that are easy to get wrong:
`mirostat_tau` ↔ `--mirostat-ent` (entropy), `mirostat_eta` ↔ `--mirostat-lr`
(learning rate), and `dynatemp_exponent` ↔ `--dynatemp-exp`.

### 8c. Starting points

**Agentic / tool calling / code — Kōdo's default workload.** Determinism and
format-correctness beat variety.

```
temperature       0.0 – 0.2
top_k             0          (let min_p truncate)
top_p             1.0
min_p             0.05
repeat_penalty    1.0        (off — see §5)
presence_penalty  0.0
frequency_penalty 0.0
xtc_probability   0.0        (off — see §6)
```

**Balanced conversation.**

```
temperature 0.7   top_p 0.9   min_p 0.05   top_k 40
```

**Creative writing.**

```
temperature 1.0   min_p 0.05   dry_multiplier 0.8   xtc_probability 0.5   xtc_threshold 0.1
```

**A model looping verbatim.** Reach for DRY before `repeat_penalty`:

```
dry_multiplier 0.8   dry_base 1.75   dry_allowed_length 4
```

The GGUF publisher's own recommended settings (usually in the HF model card)
beat all of the above — start there and adjust.

---

## 9. How Kōdo layers this

Two layers, evaluated in order, the second one wins:

1. **The flavor's CLI args** (`LlamaFlavor.llama_args`, LLM_REGISTRY.md §4.6) —
   free-text `--flag value` lines. These become the server's launch-time
   defaults for every request that omits the field. Changing them requires
   editing the flavor and **restarting `llama-server`**.

   The flavor editor's structured sampling-defaults form (curated + advanced,
   same grouping as the session modal) is **not** a second, request-level
   layer — it is a friendlier way to edit a subset of `llama_args` itself.
   Typing a value into "Temperature" writes `--temp <value>` into the launch
   arguments text box, and vice versa: the two views are kept in sync live, in
   both directions, so they can never disagree. This is why a flavor's
   sampling knobs always require a llama-server restart, exactly like every
   other launch arg — there is deliberately no "flavor default that applies
   without restarting", since that would make some launch-config edits hot
   and others cold for no principled reason. `min_keep` has no CLI
   equivalent (`cli_flags: []`) and is therefore never offered in the flavor
   editor — see the reserved table below.
2. **The session's per-quant overrides** — what the user edits in the sampling
   modal (the ⚙ button in the chat footer, between attach and stop). Stored per
   session, keyed by local registry entry name, so switching models and
   switching back restores what was set for that quant. These *are* genuinely
   request-level and hot: moving a slider here takes effect on the very next
   request, no restart, because it rides the `POST /v1/chat/completions` body
   rather than the launch command line.

**Unset means unset.** Any parameter left blank in the session modal is
omitted from the request body entirely, so whatever the active flavor's
`llama_args` launched the server with governs it. Clearing a field in the
modal is therefore a real operation — it does not reset to "the flavor's
number", it removes the field from the wire.

**Reserved — never settable from the flavor editor or the session modal:**

| Reserved | Why |
|---|---|
| `max_tokens` / `n_predict` | Computed per request from the session's thinking tier; a user value can starve the Qwen reasoning-budget mechanism of headroom and truncate turns mid-thought (LOCAL_INFERENCE.md §2a) |
| `json_schema` | Already carried by `response_format` for structured calls |
| `grammar` | Collides with `--jinja`'s lazy tool-call grammar (LOCAL_INFERENCE.md §2) |
| `ignore_eos` | Would prevent any turn from ending cleanly |
| `logit_bias` | Needs model-specific token IDs, not obtainable from the UI |
| `n_probs`, `post_sampling_probs` | Response-shape debugging; Kōdo ignores the extra fields |
| `min_keep` (flavor editor only) | No CLI flag exists, so a flavor-level value could never take effect — session-override only, where it rides the request body directly |
| `--reasoning-budget`, `--reasoning-budget-message` | Existing `RESERVED_REASONING_CAP_ARGS`, stripped from any flavor's `llama_args` (LLM_REGISTRY.md §4.6) |

**A session override on top of a flavor's CLI arg is not a conflict** — it is
the intended mechanism. `llama-server` starts each request from its launch
config and overwrites only the fields the request body sends, so the session
override simply wins for as long as it is set, while the CLI value keeps
governing any other client pointed at that server. Nothing here needs a
warning, unlike the (removed) case of a flavor setting the same knob twice —
that can no longer happen, since the flavor editor's two views of
`llama_args` are always in sync.

**Cloud models are unaffected.** These are `llama-server` parameters; the
sampling button is not rendered at all while the session's active model is
cloud-resident, and no sampling fields are ever added to an Anthropic request.

**Scope within a session.** Like `thinking_level` (SESSIONS.md), a session's
sampling overrides apply to *every* local LLM call the session makes — the
main turn, auto-compaction, and the `web_search` tool loop — not just the
prompt the user typed. The session titler runs its own dedicated
`llama-server` with its own fixed parameters and is unaffected.
