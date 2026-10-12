"""Behavioral tests for the K2 Horizon context-window cap and the default-context ceiling.

kodo's base ``--ctx-size 0`` makes llama.cpp allocate a GGUF's full trained
length (and stops ``--fit`` from shrinking it), so a shipped entry whose
native context is very long must cap its default launch with a knob, as
``context-k2-horizon`` does for K2-Horizon-32B's 512K.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms.local_registry import (
    KNOBS_BY_ID,
    LocalLLMEntry,
    get_local_registry,
    knob_selection_args,
    resolve_context_window,
    resolve_default_profile_args,
)

_KNOB_ID = "context-k2-horizon"

#: The longest context any shipped entry launches at by default. Above this
#: the KV cache alone outgrows most machines the catalog targets.
_MAX_DEFAULT_CONTEXT = 262_144


def _shipped_entries(kodo_dir: Path) -> list[LocalLLMEntry]:
    return list(get_local_registry(kodo_dir).values())


def _k2_entries(kodo_dir: Path) -> list[LocalLLMEntry]:
    entries = [e for e in _shipped_entries(kodo_dir) if KNOBS_BY_ID[_KNOB_ID] in e.knobs]
    assert entries, f"no shipped entry lists {_KNOB_ID!r}"
    return entries


def test_k2_horizon_entries_default_to_128k(tmp_path: Path) -> None:
    for entry in _k2_entries(tmp_path):
        args = resolve_default_profile_args(tmp_path, entry)
        assert resolve_context_window(entry, args) == 131_072, entry.name


@pytest.mark.parametrize(("option", "expected"), [("256k", 262_144), ("512k", 524_288)])
def test_k2_horizon_larger_options_reach_the_native_length(
    tmp_path: Path, option: str, expected: int
) -> None:
    for entry in _k2_entries(tmp_path):
        args = dict(entry.base_llama_args)
        args.update(knob_selection_args(entry.knobs, {_KNOB_ID: option}, entry.knob_defaults))
        assert resolve_context_window(entry, args) == expected, entry.name
        assert "--rope-scaling" not in args, entry.name


def test_no_shipped_entry_launches_above_256k_by_default(tmp_path: Path) -> None:
    for entry in _shipped_entries(tmp_path):
        args = resolve_default_profile_args(tmp_path, entry)
        assert resolve_context_window(entry, args) <= _MAX_DEFAULT_CONTEXT, entry.name
