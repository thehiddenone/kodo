"""Behavioral tests for the JSON local-LLM catalog (:mod:`kodo.llms.local_registry`).

Every ``hardcoded_hf`` entry is a ``<root>/<base_llm>/<name>.json`` file, read
from two roots that share one layout: the catalog shipped inside the package
(:data:`BUILTIN_CATALOG_DIR`) and the user's ``~/.kodo/local_llms/``
(:func:`user_catalog_dir`). A user file whose name matches a shipped entry
replaces it outright. See doc/LLM_REGISTRY.md §4.

Unlike test_llm_profiles.py, nothing here swaps the shipped catalog for a toy
one — the point is that the real shipped files can be copied, edited and read
back from the user root.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from kodo.llms.local_registry import (
    BASE_LLAMA_ARGS,
    BUILTIN_CATALOG_DIR,
    SHARED_KNOBS,
    LocalLLMEntry,
    add_local_entry,
    get_knob_selections,
    get_local_registry,
    prune_unknown_model_state,
    remove_local_entry,
    set_knobs,
    user_catalog_dir,
)

#: Every shipped catalog file, one test parameter each.
_SHIPPED_FILES: tuple[Path, ...] = tuple(sorted(BUILTIN_CATALOG_DIR.glob("*/*.json")))

#: One shipped file to use wherever any valid entry will do.
_SAMPLE_FILE = _SHIPPED_FILES[0]


def _read(path: Path) -> dict[str, object]:
    body = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(body, dict)
    return body


def _write(path: Path, body: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def _user_file(kodo_dir: Path, base_llm: str, name: str) -> Path:
    return user_catalog_dir(kodo_dir) / base_llm / f"{name}.json"


def _copy_to_user_catalog(kodo_dir: Path, shipped_file: Path) -> Path:
    """Copy *shipped_file* verbatim into the user catalog, at the same relative path."""
    target = _user_file(kodo_dir, shipped_file.parent.name, shipped_file.stem)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(shipped_file, target)
    return target


def _user_only_entry(kodo_dir: Path, base_llm: str = "My-Family", name: str = "my-model") -> Path:
    """A valid user-only entry: a shipped file's body under a name no shipped entry has."""
    assert name not in get_local_registry(kodo_dir)
    return _write(_user_file(kodo_dir, base_llm, name), _read(_SAMPLE_FILE))


# ---------------------------------------------------------------------------
# A user file replaces the shipped entry of the same name
# ---------------------------------------------------------------------------


def test_the_shipped_catalog_is_not_empty() -> None:
    assert _SHIPPED_FILES
    assert BUILTIN_CATALOG_DIR.is_dir()


@pytest.mark.parametrize(
    "shipped_file", _SHIPPED_FILES, ids=[f"{p.parent.name}/{p.stem}" for p in _SHIPPED_FILES]
)
def test_an_edited_user_copy_of_a_shipped_entry_is_what_the_registry_serves(
    shipped_file: Path, tmp_path: Path
) -> None:
    """Copy a shipped file into ~/.kodo/local_llms/, edit it, and read it back.

    The new description names the user file's own path, so the registry can
    only return it if it read that exact file. Comparing the *whole* entry to
    the shipped one with just that field swapped proves the rest of the copy
    was parsed too (not one field patched over the shipped entry). Deleting
    the copy brings the shipped entry back.
    """
    name = shipped_file.stem
    shipped = get_local_registry(tmp_path)[name]
    user_file = _copy_to_user_catalog(tmp_path, shipped_file)
    marker = f"read from {user_file}"
    assert shipped.description != marker
    body = _read(user_file)
    body["description"] = marker
    _write(user_file, body)

    served = get_local_registry(tmp_path)[name]

    assert served.description == marker
    assert served == replace(shipped, description=marker)

    user_file.unlink()
    assert get_local_registry(tmp_path)[name] == shipped


def test_a_verbatim_user_copy_changes_nothing(tmp_path: Path) -> None:
    before = get_local_registry(tmp_path)
    _copy_to_user_catalog(tmp_path, _SAMPLE_FILE)

    assert get_local_registry(tmp_path) == before


def test_a_user_copy_can_change_launch_relevant_fields_too(tmp_path: Path) -> None:
    """Not just metadata: knobs, knob defaults and base args come from the user file."""
    user_file = _copy_to_user_catalog(tmp_path, _SAMPLE_FILE)
    body = _read(user_file)
    body["knobs"] = [knob.id for knob in SHARED_KNOBS]
    body["mtp_supported"] = False
    body["knob_defaults"] = {"temperature": "low"}
    body["base_llama_args"] = {"--threads": "6"}
    _write(user_file, body)

    served = get_local_registry(tmp_path)[_SAMPLE_FILE.stem]

    assert served.knobs == SHARED_KNOBS
    assert served.knob_defaults == {"temperature": "low"}
    assert served.base_llama_args == {**BASE_LLAMA_ARGS, "--threads": "6"}
    assert get_knob_selections(tmp_path, served)["temperature"] == "low"


# ---------------------------------------------------------------------------
# User-only entries
# ---------------------------------------------------------------------------


def test_a_user_only_file_adds_a_model_named_by_its_path(tmp_path: Path) -> None:
    _user_only_entry(tmp_path, base_llm="My-Family", name="my-model")

    entry = get_local_registry(tmp_path)["my-model"]

    assert entry.name == "my-model"
    assert entry.base_llm == "My-Family"
    assert entry.kind == "hardcoded_hf"


def test_a_user_only_model_still_carries_every_shipped_entry(tmp_path: Path) -> None:
    shipped = set(get_local_registry(tmp_path))
    _user_only_entry(tmp_path)

    assert set(get_local_registry(tmp_path)) == shipped | {"my-model"}


# ---------------------------------------------------------------------------
# Display order: families A-Z, biggest quant first
# ---------------------------------------------------------------------------


def _size_gb(size_hint: str) -> float:
    number, unit = size_hint.split()
    return float(number) * {"MB": 1e-3, "GB": 1.0, "TB": 1e3}[unit]


def test_the_registry_lists_families_alphabetically_and_quants_biggest_first(
    tmp_path: Path,
) -> None:
    entries = list(get_local_registry(tmp_path).values())

    families = [e.base_llm for e in entries]
    distinct = list(dict.fromkeys(families))
    assert len(distinct) == len(set(families)), "each family's entries must be contiguous"
    assert distinct == sorted(distinct, key=str.casefold)
    for family in distinct:
        sizes = [_size_gb(e.size_hint) for e in entries if e.base_llm == family]
        assert sizes == sorted(sizes, reverse=True), family


def test_a_user_only_family_is_slotted_in_alphabetically(tmp_path: Path) -> None:
    _user_only_entry(tmp_path, base_llm="aaa-first", name="aaa-model")
    _user_only_entry(tmp_path, base_llm="zzz-last", name="zzz-model")

    names = list(get_local_registry(tmp_path))

    assert names[0] == "aaa-model"
    assert names[-1] == "zzz-model"


def test_custom_entries_follow_every_catalog_entry(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="aaa-custom", kind="custom_server_url", url="http://localhost:1"),
    )
    _user_only_entry(tmp_path, base_llm="zzz-last", name="zzz-model")

    names = list(get_local_registry(tmp_path))

    assert names[-1] == "aaa-custom"
    assert names[-2] == "zzz-model"


# ---------------------------------------------------------------------------
# Invalid user files: skipped, shipped entry kept, purge suspended
# ---------------------------------------------------------------------------


def _set(key: str, value: object) -> Callable[[dict[str, object]], object]:
    def corrupt(body: dict[str, object]) -> object:
        body[key] = value
        return body

    return corrupt


def _drop(key: str) -> Callable[[dict[str, object]], object]:
    def corrupt(body: dict[str, object]) -> object:
        del body[key]
        return body

    return corrupt


def _flip_mtp(body: dict[str, object]) -> object:
    body["mtp_supported"] = not body.get("mtp_supported", False)
    return body


_CORRUPTIONS: dict[str, Callable[[dict[str, object]], object]] = {
    "not-an-object": lambda body: [body],
    "typo-in-a-key": _set("descripton", "x"),
    "carries-its-own-name": _set("name", "something-else"),
    "carries-its-own-base-llm": _set("base_llm", "Other"),
    "missing-knobs": _drop("knobs"),
    "missing-repo-id": _drop("repo_id"),
    "empty-filename": _set("filename", ""),
    "unknown-knob-id": _set("knobs", ["kv-cache", "no-such-knob"]),
    "knob-listed-twice": _set("knobs", ["kv-cache", "kv-cache"]),
    "mtp-flag-disagrees-with-knobs": _flip_mtp,
    "knob-default-for-unlisted-knob": _set("knob_defaults", {"no-such-knob": "x"}),
    "knob-default-not-an-option": _set("knob_defaults", {"kv-cache": "no-such-option"}),
    "string-where-int-expected": _set("context_window", "262144"),
    "bool-where-int-expected": _set("min_memory", True),
    "negative-int": _set("memory", -1),
    "non-string-arg-value": _set("base_llama_args", {"--threads": 6}),
}


@pytest.mark.parametrize("corruption", sorted(_CORRUPTIONS))
def test_an_invalid_user_copy_is_skipped_and_the_shipped_entry_stays(
    corruption: str, tmp_path: Path
) -> None:
    body = _read(_SAMPLE_FILE)
    knobs = body["knobs"]
    assert isinstance(knobs, list) and "kv-cache" in knobs, (
        "the corruptions assume the sample entry lists the kv-cache knob"
    )
    shipped = get_local_registry(tmp_path)[_SAMPLE_FILE.stem]
    _write(
        _user_file(tmp_path, _SAMPLE_FILE.parent.name, _SAMPLE_FILE.stem),
        _CORRUPTIONS[corruption](body),
    )

    assert get_local_registry(tmp_path)[_SAMPLE_FILE.stem] == shipped
    # Proves the file was rejected rather than accepted-and-ignored: only a
    # file that failed to load makes the purge stand down.
    assert prune_unknown_model_state(tmp_path, ["gone-model"]) == ()


def test_a_user_file_that_is_not_json_is_skipped(tmp_path: Path) -> None:
    shipped = get_local_registry(tmp_path)[_SAMPLE_FILE.stem]
    path = _user_file(tmp_path, _SAMPLE_FILE.parent.name, _SAMPLE_FILE.stem)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    assert get_local_registry(tmp_path)[_SAMPLE_FILE.stem] == shipped


def test_one_name_defined_in_two_directories_loads_neither(tmp_path: Path) -> None:
    _user_only_entry(tmp_path, base_llm="Family-A", name="twice")
    _write(_user_file(tmp_path, "Family-B", "twice"), _read(_SAMPLE_FILE))

    assert "twice" not in get_local_registry(tmp_path)


def test_non_json_and_hidden_files_are_ignored_without_suspending_the_purge(
    tmp_path: Path,
) -> None:
    family = user_catalog_dir(tmp_path) / "My-Family"
    family.mkdir(parents=True)
    (family / "README.md").write_text("# notes\n", encoding="utf-8")
    (family / ".draft.json").write_text("{ not json", encoding="utf-8")
    (user_catalog_dir(tmp_path) / ".hidden").mkdir()
    shipped = get_local_registry(tmp_path)

    assert get_local_registry(tmp_path) == shipped
    assert prune_unknown_model_state(tmp_path, ["gone-model"]) == ("gone-model",)


def _broken_user_only_entry(kodo_dir: Path) -> None:
    _write(_user_file(kodo_dir, "My-Family", "my-model"), {"description": "no knobs"})


def _stray_root_level_file(kodo_dir: Path) -> None:
    _write(user_catalog_dir(kodo_dir) / "my-model.json", _read(_SAMPLE_FILE))


def _duplicate_user_only_entry(kodo_dir: Path) -> None:
    _user_only_entry(kodo_dir, base_llm="Family-A", name="my-model")
    _write(_user_file(kodo_dir, "Family-B", "my-model"), _read(_SAMPLE_FILE))


@pytest.mark.parametrize(
    "break_catalog",
    [_broken_user_only_entry, _stray_root_level_file, _duplicate_user_only_entry],
    ids=["invalid-file", "file-outside-a-model-directory", "same-name-twice"],
)
def test_a_user_catalog_problem_suspends_the_unknown_model_purge(
    break_catalog: Callable[[Path], None], tmp_path: Path
) -> None:
    """A typo must not cost the user a model they only defined in that file."""
    _user_only_entry(tmp_path)
    set_knobs(tmp_path, "my-model", {"temperature": "low"})
    (user_catalog_dir(tmp_path) / "My-Family" / "my-model.json").unlink()
    break_catalog(tmp_path)

    assert prune_unknown_model_state(tmp_path, ["my-model"]) == ()

    stored = json.loads((tmp_path / "etc" / "local-llm-registry.json").read_text())
    assert stored["knob_selections"]["my-model"] == {"temperature": "low"}


def test_a_valid_user_only_entry_is_known_to_the_purge(tmp_path: Path) -> None:
    _user_only_entry(tmp_path)
    set_knobs(tmp_path, "my-model", {"temperature": "low"})

    assert prune_unknown_model_state(tmp_path, ["my-model"]) == ()


def test_deleting_a_user_only_entry_lets_the_purge_forget_it(tmp_path: Path) -> None:
    path = _user_only_entry(tmp_path)
    set_knobs(tmp_path, "my-model", {"temperature": "low"})
    path.unlink()

    assert prune_unknown_model_state(tmp_path, ["my-model"]) == ("my-model",)


# ---------------------------------------------------------------------------
# Custom-entry CRUD against user catalog names
# ---------------------------------------------------------------------------


def test_a_custom_entry_cannot_take_a_user_catalog_name(tmp_path: Path) -> None:
    _user_only_entry(tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        add_local_entry(
            tmp_path,
            LocalLLMEntry(name="my-model", kind="custom_server_url", url="http://localhost:1"),
        )


def test_a_user_catalog_entry_is_removed_by_deleting_its_file_not_via_the_api(
    tmp_path: Path,
) -> None:
    _user_only_entry(tmp_path)

    with pytest.raises(ValueError, match=re.escape(str(user_catalog_dir(tmp_path)))):
        remove_local_entry(tmp_path, "my-model")
    assert "my-model" in get_local_registry(tmp_path)
