"""The Model Importer's catalog service (kodo.llms.model_import.LocalCatalogService).

Each test runs against a temporary ``~/.kodo`` and a fake Hugging Face hub, so
what is checked is the service's contract — what it derives, what it verifies
against a GGUF header, what it refuses — and that whatever it writes is served
by :func:`~kodo.llms.local_registry.get_local_registry` exactly as a
hand-written user catalog file would be. Shipped entries a test leans on are
read off the live catalog, never named, so a catalog edit cannot silently
change what a test exercises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kodo.llms.local import GgufMetadata
from kodo.llms.local_registry import (
    MTP_HEAD_MIN_LLAMACPP_VERSION,
    MTP_SIDECARS_FILENAME,
    builtin_catalog_entries,
    builtin_mtp_sidecars,
    get_local_registry,
    get_mtp_sidecars,
    user_catalog_dir,
)
from kodo.llms.model_import import (
    HF_KNOWN_PUBLISHERS,
    HF_TOP_PUBLISHERS,
    SEARCH_MIN_QUERY_LENGTH,
    LocalCatalogService,
    ModelImportError,
    RepoFile,
    RepoSearchHit,
    RepoSnapshot,
    format_size_hint,
    guess_quant_type,
)

_REPO = "acme/Testmodel-7B-GGUF"
_FAMILY = "Testmodel-7B"
_ARCH = "testarch"  # no context knob targets it


def _meta(
    arch: str = _ARCH, *, context: int = 131072, nextn: int = 0, shared: bool = False
) -> GgufMetadata:
    return GgufMetadata(
        version=3,
        tensor_count=300,
        architecture=arch,
        name="Testmodel 7B",
        size_label="7B",
        file_type=15,
        block_count=32,
        context_length=context,
        nextn_predict_layers=nextn,
        shared_target_tensors=shared,
        expert_count=0,
        expert_used_count=0,
        split_count=0,
        has_chat_template=True,
        scalars={},
        arrays={},
    )


def _snapshot(repo_id: str, files: dict[str, int]) -> RepoSnapshot:
    return RepoSnapshot(
        repo_id=repo_id,
        author=repo_id.split("/")[0],
        gated="",
        license="apache-2.0",
        license_name="",
        license_link="",
        base_models=("acme/Testmodel-7B",),
        tags=(),
        pipeline_tag="text-generation",
        readme="# Testmodel",
        readme_truncated=False,
        files=tuple(RepoFile(path=p, size=n) for p, n in files.items()),
    )


_FILES = {
    "README.md": 1_000,
    "Testmodel-7B-Q4_K_M.gguf": 4_400_000_000,
    "Testmodel-7B-Q8_0.gguf": 7_700_000_000,
    "BF16/Testmodel-7B-BF16-00001-of-00002.gguf": 9_000_000_000,
    "BF16/Testmodel-7B-BF16-00002-of-00002.gguf": 5_500_000_000,
    "mmproj-F16.gguf": 900_000_000,
    "MTP/mtp-Testmodel-7B-Q8_0.gguf": 420_000_000,
    "MTP/mtp-Testmodel-7B-shared-Q8_0.gguf": 210_000_000,
}


class _FakeHub:
    """Serves canned snapshots, headers and search hits; counts reads."""

    def __init__(self) -> None:
        self.snapshots: dict[str, RepoSnapshot] = {_REPO: _snapshot(_REPO, _FILES)}
        self.headers: dict[tuple[str, str], GgufMetadata] = {
            (_REPO, "Testmodel-7B-Q4_K_M.gguf"): _meta(),
            (_REPO, "Testmodel-7B-Q8_0.gguf"): _meta(),
            (_REPO, "BF16/Testmodel-7B-BF16-00001-of-00002.gguf"): _meta(),
            (_REPO, "MTP/mtp-Testmodel-7B-Q8_0.gguf"): _meta(nextn=1),
            (_REPO, "MTP/mtp-Testmodel-7B-shared-Q8_0.gguf"): _meta(nextn=1, shared=True),
        }
        self.header_reads = 0
        self.search_hits: tuple[RepoSearchHit, ...] = ()
        self.search_error = ""
        self.searches: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int) -> tuple[RepoSearchHit, ...]:
        self.searches.append((query, limit))
        if self.search_error:
            raise ModelImportError(self.search_error)
        return self.search_hits[:limit]

    async def snapshot(self, repo_id: str) -> RepoSnapshot:
        if repo_id not in self.snapshots:
            raise ModelImportError(f"Hugging Face repository not found: {repo_id}")
        return self.snapshots[repo_id]

    async def gguf_header(self, repo_id: str, filename: str) -> GgufMetadata:
        self.header_reads += 1
        if (repo_id, filename) not in self.headers:
            raise ModelImportError(f"{repo_id}/{filename}: not found")
        return self.headers[(repo_id, filename)]


@pytest.fixture
def hub() -> _FakeHub:
    return _FakeHub()


@pytest.fixture
def service(tmp_path: Path, hub: _FakeHub) -> LocalCatalogService:
    return LocalCatalogService(tmp_path, hub=hub)


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "base_llm": _FAMILY,
        "name": "acme-testmodel-7b-q4-k-m",
        "repo_id": _REPO,
        "filename": "Testmodel-7B-Q4_K_M.gguf",
        "quant_type": "Q4_K_M",
        "description": "Testmodel 7B Q4_K_M by acme",
        "quant_author": "acme",
        "llm_author": "Acme Labs",
        "license_name": "Apache License 2.0",
        "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "gpu_tip": "~13GB total at 128K context.",
        "mac_tip": "Needs ~13GB — fits a 16GB Mac with Apple Silicon.",
        "llamacpp_version": 9000,
        "min_memory": 16,
        "memory": 16,
        "builtin_mtp": False,
        "context_knob": "",
    }
    fields.update(overrides)
    return fields


async def _context_knob(service: LocalCatalogService) -> dict[str, object]:
    """The first context knob the catalog offers, as ``list_local_llms`` reports it."""
    knobs = (await service.list_catalog())["context_knobs"]
    assert isinstance(knobs, list) and knobs, "the catalog offers no context knob"
    knob = knobs[0]
    assert isinstance(knob, dict)
    return knob


def _list(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return value


def _user_files(kodo_dir: Path) -> list[Path]:
    root = user_catalog_dir(kodo_dir)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


# ---------------------------------------------------------------------------
# add_quant — what reaches the catalog
# ---------------------------------------------------------------------------


async def test_an_added_quant_is_served_by_the_registry(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    result = await service.add_quant(_fields())
    served = get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"]
    assert served.base_llm == _FAMILY
    assert served.kind == "hardcoded_hf"
    assert served.repo_id == _REPO
    assert served.description == "Testmodel 7B Q4_K_M by acme"
    assert result["path"] == str(
        user_catalog_dir(tmp_path) / _FAMILY / "acme-testmodel-7b-q4-k-m.json"
    )


async def test_size_and_context_come_from_the_hub_and_the_header_not_the_caller(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(_fields())
    served = get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"]
    assert served.size_hint == "4.40 GB"
    assert served.context_window == 131072


async def test_a_split_quant_is_sized_by_all_its_shards(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(
        _fields(
            name="acme-testmodel-7b-bf16",
            filename="BF16/Testmodel-7B-BF16-00001-of-00002.gguf",
            quant_type="BF16",
        )
    )
    assert get_local_registry(tmp_path)["acme-testmodel-7b-bf16"].size_hint == "14.5 GB"


async def test_a_16_bit_quant_defaults_to_an_f16_kv_cache_and_others_do_not(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(_fields())
    await service.add_quant(
        _fields(
            name="acme-testmodel-7b-bf16",
            filename="BF16/Testmodel-7B-BF16-00001-of-00002.gguf",
            quant_type="BF16",
        )
    )
    registry = get_local_registry(tmp_path)
    assert registry["acme-testmodel-7b-bf16"].knob_defaults == {"kv-cache": "f16"}
    assert registry["acme-testmodel-7b-q4-k-m"].knob_defaults == {}


async def test_a_quant_gets_the_shared_knobs_and_no_mtp_knob_without_mtp_layers(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    result = await service.add_quant(_fields())
    assert result["knobs"] == [
        "kv-cache",
        "tail-culling",
        "temperature",
        "gpu-layers",
        "cpu-moe",
        "nucleus-sampling",
    ]
    assert get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"].mtp_supported is False


async def test_a_quant_with_mtp_layers_gets_the_builtin_mtp_knob(
    service: LocalCatalogService, hub: _FakeHub, tmp_path: Path
) -> None:
    hub.headers[(_REPO, "Testmodel-7B-Q4_K_M.gguf")] = _meta(nextn=1)
    result = await service.add_quant(_fields(builtin_mtp=True))
    served = get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"]
    assert served.mtp_supported is True
    assert "spec-decoding-mtp" in result["knobs"]


async def test_a_quant_whose_architecture_has_a_context_knob_gets_it(
    service: LocalCatalogService, hub: _FakeHub, tmp_path: Path
) -> None:
    knob = await _context_knob(service)
    hub.headers[(_REPO, "Testmodel-7B-Q4_K_M.gguf")] = _meta(str(knob["architecture"]))
    result = await service.add_quant(_fields(context_knob=knob["id"]))
    assert knob["id"] in result["knobs"]
    served = get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"]
    assert knob["id"] in [k.id for k in served.knobs]


async def test_the_file_is_written_in_the_shipped_catalogs_canonical_form(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    result = await service.add_quant(_fields(description="Testmodel 7B — Q4_K_M"))
    text = Path(str(result["path"])).read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert "—" in text  # written with ensure_ascii=False, like the shipped files
    # The key order every shipped file uses.
    assert list(json.loads(text)) == [
        "description",
        "repo_id",
        "filename",
        "quant_author",
        "quant_type",
        "size_hint",
        "llm_author",
        "license_name",
        "license_url",
        "gpu_tip",
        "mac_tip",
        "context_window",
        "llamacpp_version",
        "min_memory",
        "memory",
        "mtp_supported",
        "knobs",
        "knob_defaults",
    ]


async def test_reading_a_header_then_adding_the_quant_fetches_the_header_once(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    await service.gguf_header(_REPO, "Testmodel-7B-Q4_K_M.gguf")
    await service.add_quant(_fields())
    assert hub.header_reads == 1


# ---------------------------------------------------------------------------
# add_quant — refusals (nothing is written)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("claimed", "layers"), [(True, 0), (False, 1)])
async def test_an_mtp_claim_the_header_contradicts_is_refused(
    service: LocalCatalogService, hub: _FakeHub, tmp_path: Path, claimed: bool, layers: int
) -> None:
    hub.headers[(_REPO, "Testmodel-7B-Q4_K_M.gguf")] = _meta(nextn=layers)
    with pytest.raises(ModelImportError, match=f"pass builtin_mtp={str(not claimed).lower()}"):
        await service.add_quant(_fields(builtin_mtp=claimed))
    assert _user_files(tmp_path) == []


async def test_a_context_knob_for_another_architecture_is_refused(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    knob = await _context_knob(service)
    with pytest.raises(ModelImportError, match="llama.cpp would ignore it"):
        await service.add_quant(_fields(context_knob=knob["id"]))
    assert _user_files(tmp_path) == []


async def test_leaving_out_the_context_knob_an_architecture_has_is_refused(
    service: LocalCatalogService, hub: _FakeHub, tmp_path: Path
) -> None:
    knob = await _context_knob(service)
    hub.headers[(_REPO, "Testmodel-7B-Q4_K_M.gguf")] = _meta(str(knob["architecture"]))
    with pytest.raises(ModelImportError, match=f"pass context_knob='{knob['id']}'"):
        await service.add_quant(_fields())
    assert _user_files(tmp_path) == []


async def test_an_unknown_context_knob_is_refused(service: LocalCatalogService) -> None:
    with pytest.raises(ModelImportError, match="unknown context knob"):
        await service.add_quant(_fields(context_knob="context-nope"))


async def test_a_shipped_entrys_name_is_refused_so_it_is_never_shadowed(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    shipped = builtin_catalog_entries()[0]
    with pytest.raises(ModelImportError, match="shipped catalog entry"):
        await service.add_quant(_fields(name=shipped.name))
    assert _user_files(tmp_path) == []


async def test_a_name_already_in_the_user_catalog_is_refused(
    service: LocalCatalogService,
) -> None:
    await service.add_quant(_fields())
    with pytest.raises(ModelImportError, match="already exists"):
        await service.add_quant(_fields(filename="Testmodel-7B-Q8_0.gguf", quant_type="Q8_0"))


async def test_a_quant_another_entry_already_serves_is_refused(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    shipped = builtin_catalog_entries()[0]
    with pytest.raises(ModelImportError, match="already served by entry"):
        await service.add_quant(
            _fields(name="acme-copy", repo_id=shipped.repo_id, filename=shipped.filename)
        )
    assert _user_files(tmp_path) == []


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("BF16/Testmodel-7B-BF16-00002-of-00002.gguf", "pass its first shard"),
        ("MTP/mtp-Testmodel-7B-Q8_0.gguf", "is an MTP head"),
        ("mmproj-F16.gguf", "vision projector"),
        ("Testmodel-7B-Q5_K_M.gguf", "not a GGUF quant in this repo"),
    ],
)
async def test_a_file_that_is_not_a_quants_first_file_is_refused(
    service: LocalCatalogService, filename: str, message: str
) -> None:
    with pytest.raises(ModelImportError, match=message):
        await service.add_quant(_fields(filename=filename))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"quant_type": "fancy"}, "no bit width"),
        ({"min_memory": 32, "memory": 16}, "at least 'min_memory'"),
        ({"memory": -1}, "non-negative integer"),
        ({"builtin_mtp": "yes"}, "true or false"),
        ({"description": "  "}, "must not be empty"),
        ({"name": "Has Spaces"}, "lowercase"),
        ({"base_llm": "../escape"}, "base_llm"),
    ],
)
async def test_malformed_fields_are_refused(
    service: LocalCatalogService, tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ModelImportError, match=message):
        await service.add_quant(_fields(**overrides))
    assert _user_files(tmp_path) == []


def test_a_refusal_is_a_value_error_which_is_what_the_tools_catch() -> None:
    assert issubclass(ModelImportError, ValueError)


# ---------------------------------------------------------------------------
# set_mtp_heads
# ---------------------------------------------------------------------------


_HEAD = {
    "id": "q8_0",
    "repo_id": _REPO,
    "filename": "MTP/mtp-Testmodel-7B-Q8_0.gguf",
    "quant_type": "Q8_0",
}


async def test_heads_are_offered_by_the_family_with_their_size_and_minimum_build(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(_fields())
    result = await service.set_mtp_heads(_FAMILY, [_HEAD])
    assert result["added"] == ["q8_0"]
    (head,) = get_mtp_sidecars(tmp_path, _FAMILY)
    assert (head.id, head.filename, head.quant_type) == ("q8_0", _HEAD["filename"], "Q8_0")
    assert head.size_hint == "420 MB"
    assert head.llamacpp_version == MTP_HEAD_MIN_LLAMACPP_VERSION
    # The family's quants now offer the head picker in place of nothing.
    served = get_local_registry(tmp_path)["acme-testmodel-7b-q4-k-m"]
    assert any(k.id.startswith("spec-decoding-mtp-head:") for k in served.knobs)


async def test_heads_for_a_family_with_no_entries_are_refused(
    service: LocalCatalogService,
) -> None:
    with pytest.raises(ModelImportError, match="add the family's quants first"):
        await service.set_mtp_heads(_FAMILY, [_HEAD])


async def test_a_head_that_borrows_the_targets_tensors_is_refused(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(_fields())
    shared = {**_HEAD, "id": "shared", "filename": "MTP/mtp-Testmodel-7B-shared-Q8_0.gguf"}
    with pytest.raises(ModelImportError, match="nextn_shared_target_tensors"):
        await service.set_mtp_heads(_FAMILY, [shared])
    assert not (user_catalog_dir(tmp_path) / _FAMILY / MTP_SIDECARS_FILENAME).exists()


async def test_a_head_file_without_mtp_layers_is_refused(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    await service.add_quant(_fields())
    hub.headers[(_REPO, _HEAD["filename"])] = _meta(nextn=0)
    with pytest.raises(ModelImportError, match="carries no MTP layers"):
        await service.set_mtp_heads(_FAMILY, [_HEAD])


async def test_a_head_of_another_architecture_is_refused(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    await service.add_quant(_fields())
    hub.headers[(_REPO, _HEAD["filename"])] = _meta("otherarch", nextn=1)
    with pytest.raises(ModelImportError, match="family's quants are 'testarch'"):
        await service.set_mtp_heads(_FAMILY, [_HEAD])


async def test_a_quant_file_is_not_accepted_as_a_head(service: LocalCatalogService) -> None:
    await service.add_quant(_fields())
    with pytest.raises(ModelImportError, match="not an MTP head file"):
        await service.set_mtp_heads(_FAMILY, [{**_HEAD, "filename": "Testmodel-7B-Q8_0.gguf"}])


async def test_adding_a_head_already_listed_writes_nothing(
    service: LocalCatalogService, tmp_path: Path
) -> None:
    await service.add_quant(_fields())
    await service.set_mtp_heads(_FAMILY, [_HEAD])
    path = user_catalog_dir(tmp_path) / _FAMILY / MTP_SIDECARS_FILENAME
    before = path.stat().st_mtime_ns
    result = await service.set_mtp_heads(_FAMILY, [_HEAD])
    assert result["added"] == [] and result["path"] == ""
    assert path.stat().st_mtime_ns == before


async def test_a_head_id_already_naming_another_file_is_refused(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    other = "MTP/mtp-Testmodel-7B-Q4_0.gguf"
    hub.snapshots[_REPO] = _snapshot(_REPO, {**_FILES, other: 300_000_000})
    hub.headers[(_REPO, other)] = _meta(nextn=1)
    await service.add_quant(_fields())
    await service.set_mtp_heads(_FAMILY, [_HEAD])
    with pytest.raises(ModelImportError, match="pick another id"):
        await service.set_mtp_heads(_FAMILY, [{**_HEAD, "filename": other, "quant_type": "Q4_0"}])


async def test_a_family_with_shipped_heads_keeps_them_when_a_head_is_added(
    tmp_path: Path, hub: _FakeHub
) -> None:
    shipped_heads = builtin_mtp_sidecars()
    if not shipped_heads:
        pytest.skip("no shipped family has standalone MTP heads")
    family, heads = next(iter(shipped_heads.items()))
    entry = next(e for e in builtin_catalog_entries() if e.base_llm == family)
    arch = "familyarch"
    hub.headers[(entry.repo_id, entry.filename)] = _meta(arch)
    new_file = "MTP/mtp-extra-Q2_K.gguf"
    hub.snapshots[entry.repo_id] = _snapshot(entry.repo_id, {new_file: 100_000_000})
    hub.headers[(entry.repo_id, new_file)] = _meta(arch, nextn=1)
    service = LocalCatalogService(tmp_path, hub=hub)

    await service.set_mtp_heads(
        family,
        [{"id": "extra", "repo_id": entry.repo_id, "filename": new_file, "quant_type": "Q2_K"}],
    )
    offered = {h.id for h in get_mtp_sidecars(tmp_path, family)}
    assert offered == {h.id for h in heads} | {"extra"}


# ---------------------------------------------------------------------------
# model_info / list_catalog
# ---------------------------------------------------------------------------


async def test_model_info_sorts_a_repos_ggufs_into_quants_heads_and_projectors(
    service: LocalCatalogService,
) -> None:
    info = await service.model_info(_REPO)
    quants = {q["filename"]: q for q in _list(info["gguf_quants"])}
    assert set(quants) == {
        "Testmodel-7B-Q4_K_M.gguf",
        "Testmodel-7B-Q8_0.gguf",
        "BF16/Testmodel-7B-BF16-00001-of-00002.gguf",
    }
    bf16 = quants["BF16/Testmodel-7B-BF16-00001-of-00002.gguf"]
    assert (bf16["shards"], bf16["total_bytes"], bf16["quant_type_guess"]) == (
        2,
        14_500_000_000,
        "BF16",
    )
    assert bf16["precision_bits"] == 16
    assert [h["filename"] for h in _list(info["mtp_head_files"])] == [
        "MTP/mtp-Testmodel-7B-Q8_0.gguf",
        "MTP/mtp-Testmodel-7B-shared-Q8_0.gguf",
    ]
    assert info["mmproj_files"] == ["mmproj-F16.gguf"]
    assert info["base_models"] == ["acme/Testmodel-7B"]


async def test_model_info_of_a_missing_repo_is_an_import_error(
    service: LocalCatalogService,
) -> None:
    with pytest.raises(ModelImportError, match="not found"):
        await service.model_info("acme/nope")


async def test_list_catalog_shows_shipped_and_user_families_and_the_context_knobs(
    service: LocalCatalogService,
) -> None:
    await service.add_quant(_fields())
    listing = await service.list_catalog()
    families = {f["base_llm"]: f for f in _list(listing["families"])}
    ours = families[_FAMILY]
    assert ours["llamacpp_versions"] == [9000]
    assert [e["source"] for e in _list(ours["entries"])] == ["user"]
    shipped = builtin_catalog_entries()[0]
    assert shipped.base_llm in families
    assert all(k["architecture"] for k in _list(listing["context_knobs"]))


# ---------------------------------------------------------------------------
# Helpers the tools surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "quant"),
    [
        ("Qwen3.8-27B-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
        ("UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf", "UD-Q4_K_XL"),
        ("Ornith-1.5-35B-A3B-Q4_K_M.gguf", "Q4_K_M"),
        ("model-q8_0.gguf", "Q8_0"),
        ("gpt-oss-20b-MXFP4.gguf", "MXFP4"),
        ("x-UD-IQ2_XXS.gguf", "UD-IQ2_XXS"),
        ("BF16/x-BF16-00001-of-00002.gguf", "BF16"),
        ("weights.gguf", ""),
    ],
)
def test_guess_quant_type_reads_the_trailing_token(path: str, quant: str) -> None:
    assert guess_quant_type(path) == quant


@pytest.mark.parametrize(
    ("size", "hint"),
    [
        (17_559_178_144, "17.6 GB"),
        (1_369_590_656, "1.37 GB"),
        (128_400_000_000, "128 GB"),
        (904_004_000, "904 MB"),
    ],
)
def test_format_size_hint_matches_the_shipped_catalogs_style(size: int, hint: str) -> None:
    assert format_size_hint(size) == hint


# --- search_repos: the settings dialog's search-as-you-type box --------------

# One publisher per tier, read off the live lists so editing them cannot
# silently change what these tests exercise.
_TOP_AUTHOR = sorted(HF_TOP_PUBLISHERS)[0]
_KNOWN_AUTHOR = sorted(HF_KNOWN_PUBLISHERS)[0]
_OTHER_AUTHOR = "someone-else"
assert _OTHER_AUTHOR.lower() not in {a.lower() for a in HF_TOP_PUBLISHERS | HF_KNOWN_PUBLISHERS}, (
    "the 'other' fixture author must not be on either publisher list"
)


def _hit(
    repo_id: str, downloads: int, *, tags: tuple[str, ...] = (), gated: bool = False
) -> RepoSearchHit:
    return RepoSearchHit(
        repo_id=repo_id,
        downloads=downloads,
        likes=downloads // 1000,
        gated=gated,
        last_modified="2026-08-13T08:28:40.000Z",
        tags=tags,
    )


def _rows(result: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", result["results"])


def _ids(result: dict[str, object]) -> list[object]:
    return [row["repo_id"] for row in _rows(result)]


async def test_search_ranks_top_then_known_then_others_each_by_downloads(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    hub.search_hits = (
        _hit(f"{_OTHER_AUTHOR}/Big-GGUF", 9_000_000),
        _hit(f"{_KNOWN_AUTHOR}/Mid-GGUF", 5_000_000),
        _hit(f"{_TOP_AUTHOR}/Small-GGUF", 10),
        _hit(f"{_KNOWN_AUTHOR}/Low-GGUF", 1_000),
        _hit(f"{_TOP_AUTHOR}/Large-GGUF", 2_000),
        _hit(f"{_OTHER_AUTHOR}/Tiny-GGUF", 5),
    )

    result = await service.search_repos("gguf")

    assert _ids(result) == [
        f"{_TOP_AUTHOR}/Large-GGUF",
        f"{_TOP_AUTHOR}/Small-GGUF",
        f"{_KNOWN_AUTHOR}/Mid-GGUF",
        f"{_KNOWN_AUTHOR}/Low-GGUF",
        f"{_OTHER_AUTHOR}/Big-GGUF",
        f"{_OTHER_AUTHOR}/Tiny-GGUF",
    ]
    tiers = [row["publisher_tier"] for row in _rows(result)]
    assert tiers == ["top", "top", "known", "known", "other", "other"]


async def test_search_matches_publishers_ignoring_case(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    hub.search_hits = (
        _hit(f"{_OTHER_AUTHOR}/A-GGUF", 100),
        _hit(f"{_TOP_AUTHOR.swapcase()}/B-GGUF", 1),
    )

    result = await service.search_repos("gguf")

    row = _rows(result)[0]
    assert row["repo_id"] == f"{_TOP_AUTHOR.swapcase()}/B-GGUF"
    assert row["publisher_tier"] == "top"
    assert row["author"] == _TOP_AUTHOR.swapcase()


async def test_search_cuts_to_twenty_after_ranking(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    others = tuple(_hit(f"{_OTHER_AUTHOR}/M{i}-GGUF", 1_000_000 + i) for i in range(40))
    hub.search_hits = (*others, _hit(f"{_TOP_AUTHOR}/Rare-GGUF", 1))

    result = await service.search_repos("gguf")

    ids = _ids(result)
    assert len(ids) == 20
    assert ids[0] == f"{_TOP_AUTHOR}/Rare-GGUF"


async def test_search_row_carries_card_facts_from_tags(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    hub.search_hits = (
        _hit(
            f"{_KNOWN_AUTHOR}/Thing-GGUF",
            42_000,
            gated=True,
            tags=(
                "gguf",
                "base_model:quantized:acme/Thing",
                "base_model:acme/Thing",
                "license:apache-2.0",
            ),
        ),
    )

    result = await service.search_repos("thing")

    assert result["results"] == [
        {
            "repo_id": f"{_KNOWN_AUTHOR}/Thing-GGUF",
            "author": _KNOWN_AUTHOR,
            "publisher_tier": "known",
            "downloads": 42_000,
            "likes": 42,
            "gated": True,
            "last_modified": "2026-08-13T08:28:40.000Z",
            "base_model": "acme/Thing",
            "license": "apache-2.0",
            "in_catalog": False,
        }
    ]


async def test_search_flags_repos_the_registry_already_serves(
    tmp_path: Path, service: LocalCatalogService, hub: _FakeHub
) -> None:
    served = next(e.repo_id for e in get_local_registry(tmp_path).values() if e.repo_id)
    hub.search_hits = (_hit(served.upper(), 10), _hit(f"{_OTHER_AUTHOR}/New-GGUF", 5))

    result = await service.search_repos("gguf")

    flags = {row["repo_id"]: row["in_catalog"] for row in _rows(result)}
    assert flags == {served.upper(): True, f"{_OTHER_AUTHOR}/New-GGUF": False}


async def test_search_trims_the_query_and_echoes_it_as_sent(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    result = await service.search_repos("  qwen 27b ")

    assert hub.searches and hub.searches[0][0] == "qwen 27b"
    assert result["query"] == "  qwen 27b "


async def test_search_skips_the_hub_for_a_too_short_query(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    hub.search_hits = (_hit(f"{_TOP_AUTHOR}/X-GGUF", 1),)
    short = " " + "q" * (SEARCH_MIN_QUERY_LENGTH - 1) + " "

    result = await service.search_repos(short)

    assert result == {"query": short, "results": []}
    assert hub.searches == []


async def test_search_passes_hub_failures_through(
    service: LocalCatalogService, hub: _FakeHub
) -> None:
    hub.search_error = "Hugging Face search failed with HTTP 503"

    with pytest.raises(ModelImportError, match="HTTP 503"):
        await service.search_repos("qwen")
