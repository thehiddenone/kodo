"""Local CPU summarization model that powers session titling.

Replaces the old ``session_titler`` sub-agent (a full LLM turn through
:class:`~kodo.runtime._engine.WorkflowEngine`, 10-15s per call) with a small
dedicated encoder-decoder model (``Falconsai/text_summarization``, a
fine-tuned T5) run on CPU so it never competes with llama.cpp for GPU memory.

Calls ``AutoModelForSeq2SeqLM``/``AutoTokenizer`` directly rather than
``transformers.pipeline("summarization", ...)``: transformers 5.x dropped the
pipeline task-name wrappers (including ``"summarization"``) outright, and
staying on the 4.x series (which still had it) conflicts with this project's
``huggingface_hub>=1.18.0`` pin (``kodo.llms.local``, doc/LOCAL_MODEL_MANAGER.md
— an unrelated, already-shipped feature that pin cannot be relaxed for). The
model/tokenizer pair is loaded once per process and kept resident (see
:func:`_get_model`) so every call after the first is a plain forward pass,
not a reload.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import cast

from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from transformers._typing import GenerativePreTrainedModel
from transformers.modeling_utils import PreTrainedModel
from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from kodo.project import kodo_user_dir

__all__ = ["generate_title", "titler_home_dir", "warm_up_titler_cache"]

_log = logging.getLogger(__name__)

_MODEL_NAME = "Falconsai/text_summarization"

# The task prefix this T5 checkpoint was fine-tuned with (its own config.json
# -> task_specific_params.summarization.prefix). Without it the model picks the
# wrong task and sometimes *translates* the prompt (e.g. English -> German)
# instead of summarizing it. Always prepend it to the input.
_SUMMARIZE_PREFIX = "summarize: "

# Guards both lazy model construction and every inference call below. The
# model/tokenizer are single instances shared for the process lifetime;
# transformers does not document them as safe for concurrent multi-threaded
# calls, and titling is rare enough (once per session) that serializing here
# is free in practice.
_lock = threading.Lock()
_tokenizer: PreTrainedTokenizerBase | None = None
_model: PreTrainedModel | None = None


def titler_home_dir() -> Path:
    """``~/.kodo/titler`` — HuggingFace cache dir for the titling model."""
    return kodo_user_dir() / "titler"


def _get_model() -> tuple[PreTrainedTokenizerBase, PreTrainedModel]:
    """Return the process-wide (tokenizer, model) pair, loading on first use."""
    global _tokenizer, _model
    if _tokenizer is not None and _model is not None:
        return _tokenizer, _model
    with _lock:
        if _tokenizer is None or _model is None:
            home = titler_home_dir()
            home.mkdir(parents=True, exist_ok=True)
            _log.info("Loading titler model %s (cache=%s)", _MODEL_NAME, home)
            tokenizer = AutoTokenizer.from_pretrained(_MODEL_NAME, cache_dir=str(home))
            model = AutoModelForSeq2SeqLM.from_pretrained(_MODEL_NAME, cache_dir=str(home))
            model.to("cpu")
            model.eval()
            _tokenizer = tokenizer
            _model = model
    assert _tokenizer is not None
    assert _model is not None
    return _tokenizer, _model


def warm_up_titler_cache() -> None:
    """Download and load the titler model if it has not been cached yet.

    Called once at server startup, off the event loop (mirrors the existing
    ``ensure_all_utils`` first-run pattern in ``server/_app.py``), so the
    *first* real session's titling call never pays a multi-second model
    download inline. A no-op once ``~/.kodo/titler`` exists from an earlier
    run. Failures are logged and swallowed — :func:`generate_title` retries
    the load lazily on its own next call.
    """
    home = titler_home_dir()
    if home.exists():
        return
    home.mkdir(parents=True, exist_ok=True)
    try:
        _get_model()
    except Exception:
        _log.exception("Titler model warm-up failed; titling will retry lazily")


def generate_title(text: str) -> str | None:
    """Summarize *text* into a short raw title using the local CPU model.

    Blocking and CPU-bound — callers must run this off the event loop (e.g.
    via ``asyncio.to_thread``). Returns ``None`` on any failure (model not
    loadable, empty output, ...) so callers can leave the session unnamed
    rather than propagate an exception into the caller's turn.
    """
    try:
        tokenizer, model = _get_model()
        with _lock:
            inputs = tokenizer(_SUMMARIZE_PREFIX + text, return_tensors="pt", truncation=True)
            # PreTrainedModel itself doesn't declare .generate() (only
            # concrete generation-capable subclasses mix in GenerationMixin,
            # which AutoModelForSeq2SeqLM's models always do at runtime) —
            # cast to the structural Protocol transformers' own stub uses as
            # generate()'s self-type, instead of falling through to
            # nn.Module's untyped attribute lookup.
            #
            # Generation knobs (see the titler quality investigation): use
            # max_new_tokens (a real ~8-word budget) instead of the old tiny
            # max_length=16, which chopped titles mid-phrase ("The Game of ..."
            # with no subject). No min_length — a minimum floor forced the model
            # to pad trivially short prompts with repetition ("fix this fix
            # this"). Greedy is deterministic and, for these short essentially
            # extractive inputs, as good as beam search while being faster;
            # no_repeat_ngram_size kills the worst echo loops. The output is
            # clamped to a real title downstream by SessionTitler._sanitize_title.
            output_ids = cast(GenerativePreTrainedModel, model).generate(
                inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                max_new_tokens=24,
                no_repeat_ngram_size=3,
                do_sample=False,
            )
            summary = tokenizer.decode(output_ids[0], skip_special_tokens=True)
        assert isinstance(summary, str)
        return summary.strip() or None
    except Exception:
        _log.exception("Titler summarization failed")
        return None
