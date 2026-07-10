"""Behavioral tests for :mod:`kodo.titling`.

Network-free: ``AutoTokenizer``/``AutoModelForSeq2SeqLM`` are monkeypatched
to stub classes, so no real model is downloaded or loaded. Each test resets
the module-level cached (tokenizer, model) pair so tests never leak state
into one another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kodo.titling import _summarizer
from kodo.titling._summarizer import generate_title, titler_home_dir, warm_up_titler_cache


@pytest.fixture(autouse=True)
def _reset_model_cache() -> None:
    _summarizer._tokenizer = None
    _summarizer._model = None


class _StubTokenizer:
    """Stands in for a ``transformers`` ``PreTrainedTokenizerBase``."""

    def __init__(self, decoded: str = "A Short Title") -> None:
        self.decoded = decoded
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.decode_calls: list[Any] = []

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[list[int]]]:
        self.calls.append((text, kwargs))
        return {"input_ids": [[1, 2, 3]], "attention_mask": [[1, 1, 1]]}

    def decode(self, ids: Any, **kwargs: Any) -> str:
        self.decode_calls.append((ids, kwargs))
        return self.decoded


class _StubModel:
    """Stands in for a ``transformers`` ``PreTrainedModel``."""

    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.moved_to: list[str] = []
        self.eval_called = False

    def to(self, device: str) -> _StubModel:
        self.moved_to.append(device)
        return self

    def eval(self) -> _StubModel:
        self.eval_called = True
        return self

    def generate(self, input_ids: Any = None, **kwargs: Any) -> list[list[int]]:
        self.generate_calls.append({"input_ids": input_ids, **kwargs})
        return [[9, 9, 9]]


class _RaisingFromPretrained:
    """A stand-in for ``AutoTokenizer``/``AutoModelForSeq2SeqLM`` that fails to load."""

    @staticmethod
    def from_pretrained(name: str, **kwargs: Any) -> Any:
        raise RuntimeError("model not available")


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch, tokenizer: _StubTokenizer, model: _StubModel
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Monkeypatch AutoTokenizer/AutoModelForSeq2SeqLM.from_pretrained; records call kwargs."""
    tokenizer_calls: list[dict[str, Any]] = []
    model_calls: list[dict[str, Any]] = []

    class _StubAutoTokenizer:
        @staticmethod
        def from_pretrained(name: str, **kwargs: Any) -> _StubTokenizer:
            tokenizer_calls.append({"name": name, **kwargs})
            return tokenizer

    class _StubAutoModel:
        @staticmethod
        def from_pretrained(name: str, **kwargs: Any) -> _StubModel:
            model_calls.append({"name": name, **kwargs})
            return model

    monkeypatch.setattr(_summarizer, "AutoTokenizer", _StubAutoTokenizer)
    monkeypatch.setattr(_summarizer, "AutoModelForSeq2SeqLM", _StubAutoModel)
    return tokenizer_calls, model_calls


def test_titler_home_dir_is_under_kodo_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert titler_home_dir() == tmp_path / ".kodo" / "titler"


def test_generate_title_returns_decoded_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tokenizer = _StubTokenizer("Csv Export Endpoint")
    model = _StubModel()
    _install_stubs(monkeypatch, tokenizer, model)

    title = generate_title("Can you add CSV export to the reports page?")

    assert title == "Csv Export Endpoint"
    assert len(tokenizer.calls) == 1
    # The input is prefixed with the model's "summarize: " task prefix so the
    # T5 checkpoint summarizes rather than (occasionally) translating.
    assert tokenizer.calls[0][0] == "summarize: Can you add CSV export to the reports page?"
    assert len(model.generate_calls) == 1
    assert model.generate_calls[0]["max_new_tokens"] == 24
    assert model.generate_calls[0]["no_repeat_ngram_size"] == 3
    assert model.generate_calls[0]["do_sample"] is False
    # No min-length floor — it forced the model to pad short prompts with
    # repetition; short prompts are gated upstream in SessionTitler instead.
    assert "min_length" not in model.generate_calls[0]


def test_generate_title_loads_model_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tokenizer = _StubTokenizer()
    model = _StubModel()
    tokenizer_calls, model_calls = _install_stubs(monkeypatch, tokenizer, model)

    generate_title("first prompt")
    generate_title("second prompt")

    assert len(tokenizer_calls) == 1
    assert len(model_calls) == 1
    assert len(model.generate_calls) == 2


def test_generate_title_loads_model_on_cpu_with_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tokenizer = _StubTokenizer()
    model = _StubModel()
    tokenizer_calls, model_calls = _install_stubs(monkeypatch, tokenizer, model)

    generate_title("some prompt")

    assert tokenizer_calls[0]["name"] == "Falconsai/text_summarization"
    assert tokenizer_calls[0]["cache_dir"] == str(titler_home_dir())
    assert model_calls[0]["name"] == "Falconsai/text_summarization"
    assert model_calls[0]["cache_dir"] == str(titler_home_dir())
    assert model.moved_to == ["cpu"]
    assert model.eval_called is True


def test_generate_title_returns_none_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(_summarizer, "AutoTokenizer", _RaisingFromPretrained)
    monkeypatch.setattr(_summarizer, "AutoModelForSeq2SeqLM", _RaisingFromPretrained)

    assert generate_title("anything") is None


def test_generate_title_returns_none_for_blank_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _install_stubs(monkeypatch, _StubTokenizer(decoded="   "), _StubModel())

    assert generate_title("anything") is None


def test_warm_up_titler_cache_loads_model_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tokenizer_calls, model_calls = _install_stubs(monkeypatch, _StubTokenizer(), _StubModel())

    assert not titler_home_dir().exists()
    warm_up_titler_cache()

    assert titler_home_dir().exists()
    assert len(tokenizer_calls) == 1
    assert len(model_calls) == 1


def test_warm_up_titler_cache_skips_when_dir_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    tokenizer_calls, model_calls = _install_stubs(monkeypatch, _StubTokenizer(), _StubModel())
    titler_home_dir().mkdir(parents=True)

    warm_up_titler_cache()

    assert len(tokenizer_calls) == 0
    assert len(model_calls) == 0


def test_warm_up_titler_cache_swallows_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(_summarizer, "AutoTokenizer", _RaisingFromPretrained)
    monkeypatch.setattr(_summarizer, "AutoModelForSeq2SeqLM", _RaisingFromPretrained)

    warm_up_titler_cache()  # must not raise
