# Sampling for Lossy Quants — Choosing Parameters That Recover Quality

How to pick `llama-server` sampling parameters for a GGUF that has been
quantized below its original precision, and why the recommended values move the
way they do as the bit width drops.

**Read [SAMPLING.md](SAMPLING.md) first.** That document is the reference for
*what every parameter does*: the sampler chain and its ordering (§2), each
knob's mechanics (§3–§7), the neutral value that disables it (§8a), the
request-field ↔ CLI-flag mapping (§8b), and the `sensible_minimum`/
`sensible_maximum` bands that both Kōdo editors flag against (§8d). This
document does not repeat any of that. It answers a narrower question: given
that a quant has thrown away precision, which of those knobs buys it back, and
how much.

Scope note: everything here is about **decoding**. No sampler recovers
information a quantization pass destroyed — the weights are what they are. What
sampling can do is stop the *decoder* from turning the model's degraded
uncertainty into a wrong token, which is a meaningful fraction of the quality
gap in practice.

---

## 1. The short version

If you want numbers and not reasoning:

Start from llama.cpp's own defaults and move **one** knob. Two axes are worth
moving, and they are worth moving separately:

| Preset | `temperature` | `min_p` | `top_n_sigma` | When |
|---|---|---|---|---|
| **Light tail cull** | `0.8` | `0.05` | — | Default starting point |
| **Medium tail cull** | `0.8` | `0.08` | — | Occasional wrong-but-plausible tokens |
| **Strong tail cull** | `0.8` | `0.12` | `1.0` | Still wandering under medium |
| **Low temperature** | `0.3` | `0.05` | — | Format correctness failing |
| **Near-greedy** | `0.05` | `0.02` | — | Maximum format reliability |

All five also set `top_k 0`, `top_p 1.0` and `repeat_penalty 1.0` — explicitly
off, so `min_p` (plus `top_n_sigma` in the strongest preset) is the only
truncation in play. These are exactly the knob options the shared sampling knobs
ships (`kodo/llms/local_registry/_local_llm_laguna_s_21.py`, §7 below), with
**identical values on every quant**. The rest of this document is why.

**`0.8` is llama.cpp's default temperature and it is a perfectly good
baseline.** The first three presets keep it and change only truncation,
because removing bad candidates is a more precise fix than making every
candidate less likely. Reach for a lower temperature when you have a specific
reason (§3b), not as a reflex because the quant is lossy.

**Do not enable DRY, `repeat_penalty`, or any other repetition control.** On
agentic work these break tool calls outright — see §3f, which is the single
most important section here.

**One caveat that outranks the whole table:** if the GGUF's publisher states
recommended sampling settings on the model card, use those. They were measured
on the actual weights; everything here is derived from how quantization and
samplers interact in general, and none of it has been measured on Laguna
weights.

---

## 2. What quantization actually does to sampling

A quantized weight is the original weight plus an error term. Those errors
propagate forward and land, at the end, as a perturbation on the **logits** —
the pre-softmax scores the sampler sees. Two consequences matter:

**The perturbation is roughly constant in magnitude, while the signal is not.**
Wherever the model is confident — the top candidate leads the second by several
logits — a small perturbation changes nothing; the ranking survives. Wherever
the model is genuinely torn between close candidates, the same perturbation is
large enough to reorder them. So quantization damage is concentrated exactly at
the positions where the choice was already hard, and is close to invisible
where it was easy.

**Softmax turns a fixed logit error into a variable probability error.** Adding
noise to a logit that was far down the list can multiply that token's
probability several-fold while barely moving the top token's. The practical
result is a **noise floor**: at a heavily quantized bit width, a slice of the
tail carries probability mass it did not earn.

Both effects point the same way. The tail of a lossy quant's distribution is
less trustworthy than the tail of the original model, and the top of it is
nearly as trustworthy. **So: truncate harder, and do it relative to the top
token.** That single sentence is most of this document.

Two corollaries worth stating because they are counterintuitive:

- **Lower bit width does not mean "be more random to compensate."** The
  intuition that a damaged model needs more exploration is backwards — the
  damage *is* extra randomness, applied in the wrong place. Adding more on top
  compounds it.
- **Perplexity loss and agentic-task loss are not the same number.** A quant
  that costs a fraction of a percent of perplexity can cost much more than that
  on strict-format output (JSON tool calls, valid syntax), because those have
  positions where exactly one token is correct and being second-best is a total
  failure rather than a slight one. This is why the low-temperature
  recommendation gets *stronger* for coding as the quant gets worse.

---

## 3. The parameters that help, in priority order

### 3a. `min_p` — the first thing to set

`min_p` discards any token whose probability is below a fraction of the *top*
token's probability. That relative framing is precisely what §2 asks for: it
scales its cutoff with how confident the model is at this position, so it
truncates hard where the model is sure (and the tail is therefore all noise)
and stays permissive where the model is legitimately uncertain.

Raise it as the bit width drops: `0.03` at Q8/Q6, `0.05` in the Q4/Q5
mainstream, `0.08` at Q3, `0.1` at Q2 and below. The band (SAMPLING.md §8d) is
`0.01`–`0.2`; past `0.2` at most five tokens can ever clear the bar and you
have reinvented greedy decoding with extra steps.

### 3b. `temperature` — a real lever, but not the first one

Temperature divides the logits before softmax, so it scales **the quantization
error along with the signal**. At `T > 1` the noise floor is amplified relative
to the gaps between top candidates; at `T < 1` it is suppressed. So lowering
temperature genuinely does attenuate quantization noise — the mechanism is
real.

What does *not* follow is that a lossy quant needs a low temperature by
default. llama.cpp's default of **`0.8` works fine on a quantized coding
model**, and an earlier revision of this document was wrong to recommend
`0.1`–`0.2` across the board on mechanism alone. Two reasons the reflex is a
mistake:

- **Truncation is the more precise fix.** `min_p` *removes* the tail that
  quantization inflated; temperature merely makes every candidate — including
  the correct one — less likely relative to its neighbours. If the problem is
  "a bad token sometimes gets sampled," deleting it beats down-weighting it.
- **Low temperature has its own costs**: flatter, more repetitive prose, and a
  much stronger tendency to fall into loops, since the model keeps re-picking
  the locally-highest-probability continuation. Lowering temperature to avoid
  one failure mode can walk you straight into another.

Reach for it when you have a specific symptom that points at it — format
correctness failing (malformed JSON tool-call arguments, broken syntax, an
identifier that must be copied exactly from context). That is what the "Low
temperature" (`0.3`) and "Near-greedy" (`0.05`) presets are for, and both keep
truncation mild so the two axes stay separable.

`temperature 0.0` (greedy) is a legitimate choice for tool-call-heavy work. Its
real cost is that a wrong first token cannot be escaped by retrying, since the
output is deterministic.

### 3c. `top_n_sigma` — the most quantization-aware option

`top_n_sigma` keeps tokens within *n* standard deviations of the maximum
**logit**, cutting in logit space rather than probability space. Two properties
make it a good fit here:

- Quantization error is naturally described in logit units (§2), so a
  logit-space threshold is measuring in the same units as the damage.
- It is applied before the softmax, which makes it **temperature-invariant** —
  the candidate set it produces does not shift when you change temperature.
  With `min_p` or `top_p`, changing temperature silently changes how much gets
  truncated too, so the two knobs are entangled; with `top_n_sigma` they are
  not.

`1.0` is the value the technique's authors suggest and a reasonable default;
tighten toward `0.8` for severe quants, loosen toward `1.5` for near-lossless
ones where less trimming is warranted. Band: `0.5`–`2.0`.

It is a genuine alternative to `min_p`, not a replacement — running both is
also fine (whichever binds tighter at a given position wins), which is what the
§7 presets do to get a floor under the sigma cutoff.

### 3d. `top_k` — a backstop at the bottom of the range

A fixed cap on candidate count. It is unaware of the shape of the distribution,
so it is a blunter instrument than `min_p` and normally unnecessary — with
`min_p` set, `top_k` is the parameter that never binds.

It earns its place only at Q3 and below, for one specific failure: at a severe
quant some positions produce a genuinely **flat** distribution, where no token
leads by much. A relative threshold like `min_p` stops truncating there almost
by definition — everything is within the cutoff of the top token *because* the
top token is weak. `top_k 30`–`40` puts a hard ceiling under that case. Above
`0` and below the band's `200` ceiling; past ~200 it can never bind at all.

### 3e. `top_p` — leave it off

Nucleus sampling keeps the smallest set of tokens whose cumulative probability
reaches *p*. The problem for a lossy quant is that it is defined on
**cumulative mass**, so a noise floor spread thinly across many tail tokens
sums to a real fraction of the budget and drags those tokens inside the
nucleus. `min_p`, comparing each token against the leader, drops them
individually. Same intent, and `min_p` is the one whose failure mode is not
"quantization noise counts toward the quota."

Set it to `1.0` (off) explicitly and let `min_p` or `top_n_sigma` do the
truncating. Running `top_p` alongside them is not harmful, just redundant.

### 3f. The DRY family — do not use it for agentic work

**DRY breaks tool calling.** This is not a tuning caveat; it is a structural
incompatibility, and it was found the hard way: a Laguna preset that enabled
DRY made the model unable to spell out an attachment UUID, so every
`read_attachment` call failed.

The mechanism is exact. DRY penalises a token in proportion to the length of
the verbatim repeat it would extend:

```text
penalty = multiplier × base^(L − allowed_length)
```

Now consider what `read_attachment` requires. The attachment is manifested into
context as a tag the model must quote back verbatim:

```text
<ATTACHMENT ID="3fa85f64-5717-4562-b3fc-2c963f66afa6" filename="notes.txt"/>
```

To call the tool, the model has to reproduce that UUID exactly. A UUID
tokenizes into ~15–25 tokens, and none of llama.cpp's default sequence
breakers (`\n`, `:`, `"`, `*`) appear *inside* it — hyphens are not breakers —
so the whole thing is one unbroken match against the earlier occurrence. With
`multiplier 0.8`, `base 1.75`, `allowed_length 4`, the penalty on the correct
next token grows:

| Tokens into the UUID | Penalty on the correct token |
|---|---|
| 5 | 1.4 |
| 8 | 7.5 |
| 12 | 70 |
| 16 | 660 |

Logit penalties of that size are not a nudge, they are a prohibition. Somewhere
around the eighth token the correct continuation becomes impossible and the
model is *forced* to emit a wrong hex digit. No amount of retrying helps,
because the penalty is deterministic.

**And the problem generalises well beyond UUIDs.** DRY's premise is
"reproducing a long verbatim sequence from context is a bug." Agentic work is
made of exactly that: file paths, function and variable names, git SHAs, error
strings quoted back, diff hunks, a file being rewritten with one line changed.
Every one of those is a long verbatim repeat, and every one of them is
*correct*. There is no setting of `allowed_length` that separates "the model is
stuck in a loop" from "the model is correctly quoting a 36-character
identifier" — both are long exact matches against context.

The same argument disqualifies `repeat_penalty`, `presence_penalty` and
`frequency_penalty` (§3g), which are blunter still.

**What to do about looping instead.** Kōdo already handles this upstream, at
the right layer, where it can tell the difference:

- `CyclicThinkingDetector` (`kodo/runtime/_cyclic_thinking.py`) watches the
  thinking stream live and aborts the round when the tail is three back-to-back
  identical blocks, or a fuzzy near-duplicate — then nudges the agent.
- The watchdog's `tool_call_cycle` and stall detectors catch the round-level
  equivalents.

See STUCK_DETECTION.md §2.7 and §2.10. These operate on *blocks of text over
rounds*, so they can distinguish a genuine loop from a legitimately repeated
identifier — which a per-token sampler, seeing only "these N tokens matched
earlier," fundamentally cannot.

If you still want DRY for non-agentic use (creative writing with no tool
calls), the conventional settings are `dry_multiplier 0.8`, `dry_base 1.75`,
`dry_allowed_length 4`+, and `dry_penalty_last_n` bounded to `4096`–`8192`
rather than llama.cpp's whole-context `-1`. No Kōdo knob ships it.

### 3g. `repeat_penalty` — off, at every bit width

Set it to `1.0` and leave it there. It divides the logit of any token seen in
the last `repeat_last_n` positions, with **no regard for whether the repetition
is correct**. On code that means penalising `}`, `return`, `const`, every token
of a path being quoted back, and every identifier used more than once. It
becomes more tempting on a lossy quant (which loops more) and more damaging
(which has less margin to give up), which is the worst possible combination.
When repetition is the problem, use DRY (§3f), which was designed to fix
exactly this parameter's targeting.

The same logic applies to `presence_penalty` and `frequency_penalty` — both
additive on logits, both blind to correctness. Keep at `0.0`.

### 3h. What to actively avoid

| Parameter | Why not, on a lossy quant |
|---|---|
| `xtc_probability` | XTC *removes the top candidates* by design. On a quant whose top candidates are the only part of the distribution still trustworthy (§2), this deletes the signal and keeps the noise. Never enable for code or tool calls at any bit width. |
| `temperature > 1.0` | Amplifies the quantization noise floor relative to the signal (§3b). |
| `mirostat` | Targets a fixed output entropy by adjusting its cutoff per token. A quant's entropy is already inflated by noise, so the controller reads that inflation as real surprise and truncates *more* — but it is chasing a number, not the noise, and its behaviour becomes hard to reason about. It also bypasses the rest of the sampler chain. Prefer the direct truncation knobs. |
| `dynatemp_range` | Raises temperature where the model is uncertain — exactly the positions where a lossy quant is least trustworthy. It is the opposite of the §2 conclusion. |
| `top_p < 0.9` | Repetitive output that looks fine and fails on structure (SAMPLING.md §8d). |

---

## 4. Which preset to start with, by quant tier

This is **guidance, not encoded values** — the §7 presets are identical on
every quant. An earlier revision of this document baked a per-quant tier table
into the values themselves; that was speculative, unmeasured, and produced a
temperature low enough to hurt. Which preset suits a given build is a judgement
call informed by the tier below and settled by actually running it.

**Q8, Q6 — near-lossless.** The quant is close to the original weights, so
sampling is mostly about decoding *style*, not damage control. Use whatever the
model card recommends; **Light tail cull** if it says nothing.

**Q5, Q4, IQ4, MXFP4 — the mainstream band.** Where most people run. Noise is
sufficient to reorder close candidates but not to destabilise overall
behaviour. **Light tail cull**, moving to **Medium** if you see occasional
wrong-but-plausible tokens.

**Q3, IQ3 — heavy.** Degradation is visible: more formatting slips, weaker
long-range consistency. Start at **Medium tail cull**, and go to **Strong** if
output still wanders — the `top_n_sigma` stage it adds is the one that handles
the flat distributions this tier starts producing (§3d).

**Q2, IQ2, IQ1 — severe.** These builds exist to fit a model in memory at all,
and the honest framing is that a *smaller model at a higher quant is usually
the better trade*. **Strong tail cull** if you run one anyway. Structured
output — strict JSON, tool-call arguments, an identifier copied exactly from
context — is where these fail first and hardest, so verify tool calling
specifically before trusting a session; if that is what's breaking, **Low
temperature** is the preset that targets it.

**A note on "UD" / dynamic quants.** Unsloth's `UD-*` builds and similar
importance-matrix quants allocate more bits to the layers that matter most, so
their effective quality sits above what the nominal bit width suggests — a
`UD-Q3_K_XL` behaves closer to the Q4 tier than the naive Q3 label implies. The
tiers above are by nominal bit width and are therefore conservative for these;
if a UD build feels over-constrained, move one preset milder. The same holds
for an MoE model, where the always-on shared layers are typically kept at
higher precision than the experts.

---

## 5. Diagnosing which knob you need

Match the symptom, don't tune everything at once. Change one parameter, and
re-run the same prompt.

| Symptom | Likely cause | Move |
|---|---|---|
| Occasional wrong-but-plausible token; code that almost compiles | Noise floor being sampled | **Medium** → **Strong tail cull** (raise `min_p`) |
| Malformed JSON / broken tool-call arguments | Same, at positions with exactly one correct token | **Low temperature**, then **Near-greedy** |
| A UUID, path, or identifier copied back wrong | A repetition penalty is active | Turn DRY / `repeat_penalty` **off** (§3f). No amount of retrying fixes this |
| Verbatim loops, repeated paragraphs | Flat distribution + context copying | Let the watchdog handle it (§3f). If it persists, *raise* temperature — do **not** add a repetition penalty |
| Rambling, wandering off task | Too much tail admitted | **Strong tail cull** (adds `top_n_sigma`) |
| Bland, repetitive, obviously over-constrained | Truncation too tight, or temperature too low | Step back toward **Light tail cull** at `temperature 0.8` |
| Degradation only in long sessions | Not sampling — context/KV-cache quantization | Try `--cache-type-k`/`-v f16` instead of `q8_0` (§6) |

Two rows are worth calling out. The last one is the most commonly misdiagnosed
as a sampling problem. And the loop row inverts the intuitive fix: a *lower*
temperature makes loops more likely, not less, because it keeps re-selecting
the same locally-optimal continuation — so if you arrived at a loop by turning
temperature down, turn it back up rather than reaching for a penalty.

---

## 6. Adjacent knobs that are not samplers

Two launch args interact with quantization quality and are worth checking
before blaming the sampler:

**KV-cache quantization** (`--cache-type-k`, `--cache-type-v`). Every Kōdo
`kv-cache` knob defaults to `q8_0` for both, which roughly halves KV memory at a quality
cost that is small but *cumulative over context length* — it degrades long
sessions specifically. If a model is fine early in a session and unreliable
20K tokens in, test `f16` before touching any sampler. `--cache-type-v` is the
more sensitive of the two; keeping V at `f16` while K stays `q8_0` is a
reasonable middle ground where memory allows.

**Context extension** (`--rope-scaling yarn` and friends). Running a model past
its trained context length degrades it independently of quantization, and the
two compound. If you are using a 512K/1M context option on an already-lossy quant,
attribute quality problems there first.

Neither is a sampling parameter, and neither can be fixed by one.

---

## 7. How this is encoded in Kōdo

This document's recommendations ship as **knobs on every LLM's Default
profile** (LLM_REGISTRY.md §4.6), not as a fixed list of presets. Two shared
dropdowns, one per axis, defined in
`kodo/llms/local_registry/_knobs_shared.py`:

| Knob | Option | Flags |
|---|---|---|
| **Tail culling** | `off` *(default)* | *(none — llama.cpp's own `top_k 40`/`top_p 0.95` apply)* |
| | `minimal` | `--top-k 0 --top-p 1.0 --min-p 0.02` |
| | `light` | `--top-k 0 --top-p 1.0 --min-p 0.05` — §3a, the mildest explicit cull |
| | `medium` | `--top-k 0 --top-p 1.0 --min-p 0.08` |
| | `strong` | `--top-k 0 --top-p 1.0 --min-p 0.12 --top-nsigma 1.0` — §3c |
| **Temperature** | `default` *(default)* | `--temp 0.8`, llama.cpp's own |
| | `low` | `--temp 0.3` — §3b |
| | `near-greedy` | `--temp 0.05` |

Long-context extension is its own private per-model knob (§6 "Context
extension"): Laguna's `context-laguna` offers native (8192) / 512K / 1M, each
extended option writing `--rope-scaling yarn`, `--rope-scale` (target ÷
native), `--yarn-orig-ctx 8192` and
`--override-kv laguna.context_length=int:<size>`. KV-cache precision is the
shared `kv-cache` knob, `q8_0` by default.

**Two knobs, not one preset list — that is the point.** The Laguna-S-2.1
catalog used to ship this table as five predefined *flavors* per quant
("Light/Medium/Strong tail cull", "Low temperature", "Near-greedy"), laid out
so that the three culling presets shared a temperature and the two temperature
presets shared a `min_p`: each group varied exactly one axis, because a preset
that moved both at once could not tell you which one mattered. As two
independent knobs that layout is no longer a convention someone has to
maintain — it is structural, since the framework rejects two knobs that own the
same flag (LLM_REGISTRY.md §4.6). It also makes combinations reachable that
the fixed presets never offered, such as strong culling *at* a low temperature.

Four constraints on any change to these values:

1. **Composition is by knob, and knobs cannot collide.** A knob option lists
   only the flags its own axis owns; the shared base args
   (`--ctx-size 0`, `--reasoning-format auto`, `--jinja`) and every other
   knob's flags are merged in around it. This is why the culling options carry
   `--top-k 0`/`--top-p 1.0` but never `--temp`, and the temperature options
   carry nothing but `--temp`.
2. **Every value must stay inside its sensible band** (SAMPLING.md §8d). Both
   Kōdo editors flag out-of-band values with a yellow ⚠ and disable
   Apply/Save; a shipped option that trips its own guard rail would be
   incoherent, and a user copying it into a profile would be unable to save. A
   neutral/off value (`top_p 1.0`, `top_k 0`) is exempt from the ⚠ by §8a and
   is used deliberately here to make "this sampler is off on purpose" explicit
   rather than implicit.
3. **No knob may enable a repetition penalty** — not DRY, not
   `repeat_penalty`, not the presence/frequency pair (§3f). This is a hard
   rule, not a default: one of them shipped once and broke `read_attachment`
   outright. Loop handling belongs to the watchdog (STUCK_DETECTION.md
   §2.7/§2.10), which can tell a loop from a legitimately repeated identifier.
   A test asserts no shared knob's reachable flags include any of them. The
   rule binds what Kōdo *ships*: a user may still set one on their own profile
   or as a session override, since offering it in one editor and hiding it in
   the other would be arbitrary.
4. **These are launch args, so they are cold.** Changing a knob restarts
   `llama-server` (SAMPLING.md §9). For per-session experimentation use the
   sampling modal (the ⚙ in the chat footer), whose overrides are
   request-level, hot, and stored per quant — that is the right place to
   *find* good values, and a knob is the right place to keep them.

The values are **uniform across every quant and every model family**. An
earlier revision tiered them by quantization severity; that was speculative,
unmeasured, and produced a temperature (`0.1`) low enough to be a downgrade in
practice, so the tiering was removed rather than re-guessed. Which setting
suits which quant is guidance in §4, not something baked into the values.

---

## 8. Measuring a preset instead of arguing about it

Everything above is derived from how samplers and quantization interact. None
of it was measured on Laguna weights — and the DRY incident (§3f) is precisely
what that costs: a change that is correct in mechanism and catastrophic in
practice. The scenario below exists to close that gap.

**`hatch run validate attachment_report`**
(`kodo/validator/scenarios/attachment_report.py`) chains the four
things a lossy quant or a bad sampling setting tends to break:

1. **Reading an attachment.** The whole task specification lives in an attached
   `spec.md`, staged outside the workspace, so `read_attachment` is the only
   way to reach it — which requires reproducing the attachment's UUID verbatim.
   This is the direct regression test for §3f, and the RVP scores it as a
   **pass/fail gate worth the entire run**: a plausible-looking implementation
   written from a guess about what the spec probably said scores 0, not partial
   credit.
2. **Building with tests and a toolchain**, which is multi-part enough to clear
   Problem Solver's small-ask fast path and should route through `planner` into
   `toolchain_builder` and `developer` steps.
3. **Reading a produced file back off disk.** `results.json` is generated by the
   run's own code, so its contents cannot be known from the prompt.
4. **Deciding from that file's content** — the report's above/below split comes
   from numbers the model cannot guess, so a hallucinated report is detectable.

The input CSV is fixed, so the run has exactly one correct answer (region
totals 758.00 / 925.50 / 677.50 / 997.50 / 1239.75, mean 919.65, above =
{south, west, central}). Scoring is therefore objective rather than a matter of
judge taste. A test asserts the RVP's hardcoded expectations still match the
fixture, so the two cannot drift.

**To compare configurations**, re-run the scenario with different `knobs=`
(VALIDATOR.md §8a.1). Two cautions:

- **No seed is pinned, deliberately.** A single deterministic run tells you
  nothing about robustness — the failure modes here are intermittent. Run each
  preset several times and read the pass rate, not one score.
- **Change one axis at a time**, which is what the two-knob split is for
  (§7). Move `tail-culling` or `temperature`, not both, so a difference
  between two runs is attributable to one of them.

---

## 9. Further reading

- [SAMPLING.md](SAMPLING.md) — the parameter reference: mechanics, the sampler
  chain, neutral values, CLI-flag mapping, sensible bands, and how the two
  Kōdo layers stack.
- [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.6 — knobs and profiles: the knob
  framework and its no-two-knobs-share-a-flag invariant, base-args
  composition, the replace-not-merge rule for profiles, and the Configure /
  Manage-profiles UI.
- [LOCAL_INFERENCE.md](LOCAL_INFERENCE.md) — how Kōdo launches and talks to
  `llama-server`, including the reserved args no profile may set.
