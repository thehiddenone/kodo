"""Command-line harness for the session titler — a knob-tuning playground.

Runs the local summarization model (:mod:`kodo.titling._summarizer`) against
an arbitrary prompt so the generation knobs (prefix, beam count, length caps,
length penalty) can be swept by hand without spinning up a whole session. It
reuses the *same* cached model instance the server uses, and mirrors the
engine's title sanitizer so you see the final session title, not just the raw
model output.

Examples::

    # Current production knobs, show raw + final title:
    python -m kodo.titling "implement a game of tic tac toe with a CLI and tests"

    # Reproduce the old pre-fix behavior (no prefix, tiny cap, min-length floor):
    python -m kodo.titling --legacy "Let's build the Game of Life in Rust"

    # Side-by-side: old pre-fix config vs current production config:
    python -m kodo.titling --compare "why is my websocket reconnecting twice?"

    # Sweep a single knob:
    python -m kodo.titling --num-beams 6 --max-new-tokens 20 "add CSV export"

    # Read the prompt from stdin:
    echo "refactor the auth module" | python -m kodo.titling -
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, cast

from transformers._typing import GenerativePreTrainedModel

from ._summarizer import _get_model

# Title-shaping constants mirrored from
# kodo.runtime._engine._titling so the CLI reports the same final title the
# engine would persist. Kept as literals here (rather than imported) to avoid
# an upward dependency from kodo.titling into the runtime engine.
_MAX_TITLE_LEN = 60
_MIN_TITLE_WORDS = 2
_MAX_TITLE_WORDS = 8

# The prefix the T5 checkpoint was fine-tuned with (see the model's own
# config.json -> task_specific_params.summarization.prefix). Omitting it lets
# the model pick the wrong task and occasionally translate the prompt.
_SUMMARIZE_PREFIX = "summarize: "

# The old pre-investigation params (no prefix, tiny cap, min-length floor).
# Kept only so --legacy / --compare can show the before/after improvement.
_LEGACY = dict(
    prefix="",
    gen=dict(max_length=16, min_length=4, do_sample=False),
)
# The current production config (now applied in _summarizer.py): the task prefix
# (prevents the model from silently translating instead of summarizing), a
# real token budget so titles aren't chopped mid-phrase, and an anti-repetition
# guard so trivially short prompts don't yield "fix this fix this". Greedy —
# beam search added latency and sometimes *introduced* dupes ("terminal UI UI")
# on these short, essentially extractive inputs.
_RECOMMENDED = dict(
    prefix=_SUMMARIZE_PREFIX,
    gen=dict(
        max_new_tokens=24,
        no_repeat_ngram_size=3,
        do_sample=False,
    ),
)


def _summarize(text: str, *, prefix: str, gen: dict[str, Any]) -> tuple[str, float]:
    """Return (raw summary, elapsed_ms) for *text* under the given knobs."""
    tokenizer, model = _get_model()
    inputs = tokenizer(prefix + text, return_tensors="pt", truncation=True)
    start = time.time()
    output_ids = cast(GenerativePreTrainedModel, model).generate(
        inputs["input_ids"], attention_mask=inputs["attention_mask"], **gen
    )
    elapsed_ms = (time.time() - start) * 1000
    summary = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    assert isinstance(summary, str)
    return summary.strip(), elapsed_ms


def _sanitize_title(raw: str) -> str | None:
    """Sanitize raw model output into a final title.

    Mirrors ``SessionTitler._sanitize_title``: clamp to the first
    ``_MAX_TITLE_WORDS`` **words**, then to ``_MAX_TITLE_LEN`` chars. (Clamping
    by words is load-bearing — an earlier char-only clamp left 10-13 words that
    the word-count acceptability gate then rejected, dropping good titles.)
    """
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if not line:
        return None
    line = line.strip().strip("\"'`").rstrip(".").strip()
    line = " ".join(line.split())
    if not line:
        return None
    words = line.split()
    if len(words) > _MAX_TITLE_WORDS:
        words = words[:_MAX_TITLE_WORDS]
    line = " ".join(w[:1].upper() + w[1:] for w in words)
    if len(line) > _MAX_TITLE_LEN:
        line = line[:_MAX_TITLE_LEN].rstrip()
    return line or None


def _final_title(raw: str) -> str:
    """Apply the engine's sanitize + acceptability gate; describe the outcome."""
    title = _sanitize_title(raw)
    if not title:
        return "<<REJECTED: empty after sanitize>>"
    words = len(title.split())
    if words < _MIN_TITLE_WORDS:
        return f"<<REJECTED: {words} word (< {_MIN_TITLE_WORDS})>> {title!r}"
    if words > _MAX_TITLE_WORDS:
        return f"<<REJECTED: {words} words (> {_MAX_TITLE_WORDS})>> {title!r}"
    return title


def _read_prompt(raw_arg: str | None) -> str:
    if raw_arg is None or raw_arg == "-":
        return sys.stdin.read().strip()
    return raw_arg.strip()


def _run_one(text: str, *, prefix: str, gen: dict[str, Any], label: str, show_raw: bool) -> None:
    summary, elapsed_ms = _summarize(text, prefix=prefix, gen=gen)
    if show_raw:
        print(f"  [{label}] {elapsed_ms:5.0f}ms  raw   : {summary!r}")
    print(f"  [{label}] {elapsed_ms:5.0f}ms  title : {_final_title(summary)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kodo.titling",
        description="Tune and inspect the local session-titling summarizer.",
    )
    parser.add_argument("prompt", nargs="?", help="Prompt text ('-' or omitted reads stdin).")
    parser.add_argument(
        "--legacy", action="store_true", help="Use the old pre-fix knobs (before/after baseline)."
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run the old pre-fix config and the current production config side by side.",
    )
    parser.add_argument(
        "--no-prefix", action="store_true", help="Drop the 'summarize: ' task prefix."
    )
    parser.add_argument(
        "--greedy", action="store_true", help="Greedy decoding (num_beams=1, do_sample=False)."
    )
    parser.add_argument("--num-beams", type=int, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--min-new-tokens", type=int, default=None)
    parser.add_argument("--length-penalty", type=float, default=None)
    parser.add_argument("--no-repeat-ngram-size", type=int, default=None)
    parser.add_argument("--raw", action="store_true", help="Show raw model output too.")
    args = parser.parse_args(argv)

    text = _read_prompt(args.prompt)
    if not text:
        parser.error("no prompt provided (pass an argument or pipe via stdin)")

    print(f"PROMPT: {text}")

    if args.compare:
        _run_one(text, label="legacy ", show_raw=True, **_LEGACY)  # type: ignore[arg-type]
        _run_one(text, label="current", show_raw=True, **_RECOMMENDED)  # type: ignore[arg-type]
        return 0

    if args.legacy:
        _run_one(text, label="legacy", show_raw=args.raw, **_LEGACY)  # type: ignore[arg-type]
        return 0

    # Start from the recommended config, then apply any explicit knob overrides.
    prefix = "" if args.no_prefix else _SUMMARIZE_PREFIX
    gen: dict[str, Any] = dict(cast(dict[str, Any], _RECOMMENDED["gen"]))
    if args.greedy:
        gen.pop("num_beams", None)
        gen.pop("early_stopping", None)
        gen["do_sample"] = False
    if args.num_beams is not None:
        gen["num_beams"] = args.num_beams
    if args.max_new_tokens is not None:
        gen["max_new_tokens"] = args.max_new_tokens
    if args.min_new_tokens is not None:
        gen["min_new_tokens"] = args.min_new_tokens
    if args.length_penalty is not None:
        gen["length_penalty"] = args.length_penalty
    if args.no_repeat_ngram_size is not None:
        gen["no_repeat_ngram_size"] = args.no_repeat_ngram_size

    _run_one(text, label="custom", show_raw=True, prefix=prefix, gen=gen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
