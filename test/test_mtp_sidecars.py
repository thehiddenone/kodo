"""Behavioral tests for MTP sidecar heads in the local-LLM catalog.

A family directory may hold ``mtp_sidecars.json``: the standalone MTP draft
heads every quant of that model can use. Its presence replaces the built-in
MTP checkbox on every entry of the family with one head-picker dropdown
(*Off*, *Built-in* when the entry's own GGUF has the layers, then each head,
most precise first). See doc/LLM_REGISTRY.md §4.0a.

Registry-level only — downloading heads, pruning them with the last quant and
handing one to llama-server at launch are exercised against a real HTTP
fixture in test_llms_local.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.llms.local_registry import (
    BUILTIN_CATALOG_DIR,
    MTP_DRAFT_MODEL_FLAG,
    MTP_SIDECARS_FILENAME,
    LlamaKnob,
    LocalLLMEntry,
    add_local_entry,
    add_profile,
    get_knob_selections,
    get_local_registry,
    get_mtp_sidecars,
    get_selected_mtp_sidecar,
    mtp_sidecar_model_id,
    prune_unknown_model_state,
    resolve_default_profile_args,
    set_active_profile,
    set_knobs,
    stale_mtp_sidecar_model_ids,
    user_catalog_dir,
)

#: The wire id of the built-in-layers checkbox, and the prefix every head
#: picker's id starts with — both persisted in the user's knob selections.
_CHECKBOX_ID = "spec-decoding-mtp"
_PICKER_PREFIX = "spec-decoding-mtp-head:"

#: Families that ship a ``mtp_sidecars.json``, read off the shipped catalog.
_SHIPPED_HEAD_FAMILIES: tuple[str, ...] = tuple(
    sorted(path.parent.name for path in BUILTIN_CATALOG_DIR.glob(f"*/{MTP_SIDECARS_FILENAME}"))
)


@pytest.fixture
def kodo_dir(tmp_path: Path) -> Path:
    return tmp_path / "kodo"


def _picker(entry: LocalLLMEntry) -> LlamaKnob | None:
    pickers = [knob for knob in entry.knobs if knob.id.startswith(_PICKER_PREFIX)]
    assert len(pickers) <= 1, entry.name
    return pickers[0] if pickers else None


def _family(kodo_dir: Path, base_llm: str) -> list[LocalLLMEntry]:
    entries = [e for e in get_local_registry(kodo_dir).values() if e.base_llm == base_llm]
    assert entries, base_llm
    return entries


def _family_without_shipped_heads(kodo_dir: Path, *, built_in_mtp: bool) -> str:
    """A shipped family with no heads whose entries all have — or all lack — built-in MTP."""
    by_family: dict[str, set[bool]] = {}
    for entry in get_local_registry(kodo_dir).values():
        if entry.base_llm and entry.base_llm not in _SHIPPED_HEAD_FAMILIES:
            by_family.setdefault(entry.base_llm, set()).add(entry.mtp_supported)
    family = next(
        (name for name, flags in sorted(by_family.items()) if flags == {built_in_mtp}), None
    )
    assert family is not None, f"no shipped family with mtp_supported={built_in_mtp} throughout"
    return family


def _head(quant_type: str, **overrides: object) -> dict[str, object]:
    return {
        "repo_id": "acme/heads",
        "filename": f"MTP/mtp-{quant_type}.gguf",
        "quant_type": quant_type,
        **overrides,
    }


def _write_heads(kodo_dir: Path, base_llm: str, body: object) -> Path:
    path = user_catalog_dir(kodo_dir) / base_llm / MTP_SIDECARS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped catalog
# ---------------------------------------------------------------------------


def test_some_shipped_family_has_heads() -> None:
    """Guards the parametrized tests below against silently running zero cases."""
    assert _SHIPPED_HEAD_FAMILIES


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_every_entry_of_a_family_with_heads_offers_one_picker_instead_of_the_checkbox(
    kodo_dir: Path, base_llm: str
) -> None:
    heads = get_mtp_sidecars(kodo_dir, base_llm)
    assert heads
    for entry in _family(kodo_dir, base_llm):
        picker = _picker(entry)
        assert picker is not None, entry.name
        assert all(knob.id != _CHECKBOX_ID for knob in entry.knobs), entry.name
        expected = ["off"] + (["builtin"] if entry.mtp_supported else []) + [h.id for h in heads]
        assert [option.id for option in picker.options] == expected, entry.name


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_mtp_is_off_until_the_user_picks_a_head(kodo_dir: Path, base_llm: str) -> None:
    for entry in _family(kodo_dir, base_llm):
        picker = _picker(entry)
        assert picker is not None
        assert get_knob_selections(kodo_dir, entry)[picker.id] == "off"
        assert "--spec-type" not in resolve_default_profile_args(kodo_dir, entry)
        assert get_selected_mtp_sidecar(kodo_dir, entry) is None


def test_mtp_supported_always_matches_a_built_in_option(kodo_dir: Path) -> None:
    """Every served entry, headed family or not: the flag and the UI never disagree."""
    for entry in get_local_registry(kodo_dir).values():
        picker = _picker(entry)
        offers_built_in = any(k.id == _CHECKBOX_ID for k in entry.knobs) or (
            picker is not None and picker.option("builtin") is not None
        )
        assert entry.mtp_supported == offers_built_in, entry.name


def test_a_head_list_is_never_served_as_an_entry(kodo_dir: Path) -> None:
    assert Path(MTP_SIDECARS_FILENAME).stem not in get_local_registry(kodo_dir)


def test_a_family_without_heads_offers_none(kodo_dir: Path) -> None:
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=True)
    assert get_mtp_sidecars(kodo_dir, base_llm) == ()
    for entry in _family(kodo_dir, base_llm):
        assert _picker(entry) is None
        assert any(knob.id == _CHECKBOX_ID for knob in entry.knobs)


# ---------------------------------------------------------------------------
# Picking a head
# ---------------------------------------------------------------------------


def test_picking_a_head_launches_it_as_the_mtp_draft_model(kodo_dir: Path) -> None:
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=True)
    _write_heads(kodo_dir, base_llm, {"q8_0": _head("Q8_0")})
    entry = _family(kodo_dir, base_llm)[0]
    picker = _picker(entry)
    assert picker is not None

    set_knobs(kodo_dir, entry.name, {picker.id: "q8_0"})

    args = resolve_default_profile_args(kodo_dir, entry)
    assert args["--spec-type"] == "draft-mtp"
    assert args[MTP_DRAFT_MODEL_FLAG] == "mtp-Q8_0.gguf"
    selected = get_selected_mtp_sidecar(kodo_dir, entry)
    assert selected is not None and selected.id == "q8_0"


def test_the_built_in_option_drafts_without_a_head_file(kodo_dir: Path) -> None:
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=True)
    _write_heads(kodo_dir, base_llm, {"q8_0": _head("Q8_0")})
    entry = _family(kodo_dir, base_llm)[0]
    picker = _picker(entry)
    assert picker is not None

    set_knobs(kodo_dir, entry.name, {picker.id: "builtin"})

    args = resolve_default_profile_args(kodo_dir, entry)
    assert args["--spec-type"] == "draft-mtp"
    assert MTP_DRAFT_MODEL_FLAG not in args
    assert get_selected_mtp_sidecar(kodo_dir, entry) is None


def test_a_user_defined_profile_never_drafts_with_a_picked_head(kodo_dir: Path) -> None:
    """A profile's args are used verbatim — the Default profile's knobs do not apply."""
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=True)
    _write_heads(kodo_dir, base_llm, {"q8_0": _head("Q8_0")})
    entry = _family(kodo_dir, base_llm)[0]
    picker = _picker(entry)
    assert picker is not None
    set_knobs(kodo_dir, entry.name, {picker.id: "q8_0"})

    profile = add_profile(kodo_dir, entry.name, "Mine", llama_args={"--ctx-size": "4096"})
    set_active_profile(kodo_dir, entry.name, profile.id)

    assert get_selected_mtp_sidecar(kodo_dir, entry) is None


def test_a_family_without_built_in_layers_gets_no_built_in_option(kodo_dir: Path) -> None:
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=False)
    _write_heads(kodo_dir, base_llm, {"q4_0": _head("Q4_0")})
    for entry in _family(kodo_dir, base_llm):
        picker = _picker(entry)
        assert picker is not None, entry.name
        assert [option.id for option in picker.options] == ["off", "q4_0"]


def test_heads_are_listed_most_precise_first(kodo_dir: Path) -> None:
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=False)
    _write_heads(
        kodo_dir,
        base_llm,
        {
            "q4_k_m": _head("Q4_K_M"),
            "iq2_xxs": _head("IQ2_XXS"),
            "bf16": _head("BF16"),
            "q6_k": _head("Q6_K"),
            "f32": _head("F32"),
            "q8_0": _head("Q8_0"),
            "mxfp4": _head("MXFP4_MOE"),
            "q5_k_m": _head("UD-Q5_K_M"),
        },
    )

    ids = [head.id for head in get_mtp_sidecars(kodo_dir, base_llm)]

    assert ids == ["f32", "bf16", "q8_0", "q6_k", "q5_k_m", "mxfp4", "q4_k_m", "iq2_xxs"]


def test_a_checkbox_left_on_becomes_the_built_in_option_when_heads_arrive(
    kodo_dir: Path,
) -> None:
    """Gaining heads must not silently switch off MTP someone had turned on."""
    base_llm = _family_without_shipped_heads(kodo_dir, built_in_mtp=True)
    entry = _family(kodo_dir, base_llm)[0]
    set_knobs(kodo_dir, entry.name, {_CHECKBOX_ID: "on"})

    _write_heads(kodo_dir, base_llm, {"q8_0": _head("Q8_0")})
    entry = get_local_registry(kodo_dir)[entry.name]
    picker = _picker(entry)
    assert picker is not None

    assert get_knob_selections(kodo_dir, entry)[picker.id] == "builtin"
    assert resolve_default_profile_args(kodo_dir, entry)["--spec-type"] == "draft-mtp"


# ---------------------------------------------------------------------------
# The user catalog's head lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_a_user_list_replaces_the_shipped_one(kodo_dir: Path, base_llm: str) -> None:
    _write_heads(kodo_dir, base_llm, {"mine": _head("Q6_K")})

    assert [head.id for head in get_mtp_sidecars(kodo_dir, base_llm)] == ["mine"]
    for entry in _family(kodo_dir, base_llm):
        picker = _picker(entry)
        assert picker is not None
        assert picker.options[-1].id == "mine"


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_an_empty_user_list_switches_a_familys_heads_off(kodo_dir: Path, base_llm: str) -> None:
    _write_heads(kodo_dir, base_llm, {})

    assert get_mtp_sidecars(kodo_dir, base_llm) == ()
    for entry in _family(kodo_dir, base_llm):
        assert _picker(entry) is None, entry.name
        has_checkbox = any(knob.id == _CHECKBOX_ID for knob in entry.knobs)
        assert has_checkbox == entry.mtp_supported, entry.name


_BROKEN_LISTS: dict[str, object] = {
    "not-an-object": ["q8_0"],
    "unknown-key": {"q8_0": _head("Q8_0", sha256="abc")},
    "missing-filename": {"q8_0": {"repo_id": "acme/heads", "quant_type": "Q8_0"}},
    "reserved-id": {"builtin": _head("Q8_0")},
    "uppercase-id": {"Q8_0": _head("Q8_0")},
    "unknown-precision": {"x": _head("XYZ")},
    "negative-version": {"q8_0": _head("Q8_0", llamacpp_version=-1)},
}


@pytest.mark.parametrize("body", _BROKEN_LISTS.values(), ids=_BROKEN_LISTS.keys())
def test_a_broken_user_list_is_skipped_and_stops_the_purge(kodo_dir: Path, body: object) -> None:
    base_llm = _SHIPPED_HEAD_FAMILIES[0]
    shipped = get_mtp_sidecars(kodo_dir, base_llm)
    _write_heads(kodo_dir, base_llm, body)

    assert get_mtp_sidecars(kodo_dir, base_llm) == shipped
    # A broken file might be the user's only definition of something they
    # downloaded, so nothing stored is judged while it is broken.
    assert prune_unknown_model_state(kodo_dir, ["model-nobody-knows"]) == ()
    head_id = mtp_sidecar_model_id(base_llm, "anything")
    assert stale_mtp_sidecar_model_ids(kodo_dir, [head_id]) == ()


# ---------------------------------------------------------------------------
# Which downloaded heads are still needed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_heads_stay_while_any_quant_of_the_family_has_a_download(
    kodo_dir: Path, base_llm: str
) -> None:
    quant = _family(kodo_dir, base_llm)[0].name
    heads = [mtp_sidecar_model_id(base_llm, h.id) for h in get_mtp_sidecars(kodo_dir, base_llm)]

    assert stale_mtp_sidecar_model_ids(kodo_dir, [quant, *heads]) == ()


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_heads_go_with_the_last_quant_of_the_family(kodo_dir: Path, base_llm: str) -> None:
    other_family_quant = next(
        e.name for e in get_local_registry(kodo_dir).values() if e.base_llm != base_llm
    )
    heads = [mtp_sidecar_model_id(base_llm, h.id) for h in get_mtp_sidecars(kodo_dir, base_llm)]

    assert stale_mtp_sidecar_model_ids(kodo_dir, [other_family_quant, *heads]) == tuple(
        sorted(heads)
    )


@pytest.mark.parametrize("base_llm", _SHIPPED_HEAD_FAMILIES)
def test_a_head_the_family_no_longer_lists_is_stale(kodo_dir: Path, base_llm: str) -> None:
    quant = _family(kodo_dir, base_llm)[0].name
    dropped = mtp_sidecar_model_id(base_llm, "dropped-head")

    assert stale_mtp_sidecar_model_ids(kodo_dir, [quant, dropped]) == (dropped,)


def test_head_downloads_are_never_reported_as_unknown_models(kodo_dir: Path) -> None:
    """The model purge must not treat a head's record as a model kodo forgot."""
    base_llm = _SHIPPED_HEAD_FAMILIES[0]
    head = mtp_sidecar_model_id(base_llm, get_mtp_sidecars(kodo_dir, base_llm)[0].id)

    assert prune_unknown_model_state(kodo_dir, [head]) == ()


def test_a_custom_entry_cannot_take_a_head_download_id(kodo_dir: Path) -> None:
    name = mtp_sidecar_model_id("Family", "q8_0")
    entry = LocalLLMEntry(name=name, kind="custom_hf", repo_id="acme/x", filename="x.gguf")
    with pytest.raises(ValueError, match="reserved"):
        add_local_entry(kodo_dir, entry)
