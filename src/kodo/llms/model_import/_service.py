"""The Model Importer's operations: read a GGUF repo, add its quants to the user catalog.

One :class:`LocalCatalogService` backs the five importer tools
(``list_local_llms``, ``read_hf_model``, ``read_gguf_header``,
``add_local_llm_quant``, ``set_mtp_heads``). The agent decides *what* to add —
which quants, which family, the prose — and this service decides whether the
facts it states are true:

- **Derived, never taken from the agent**: an entry's ``size_hint`` (summed
  shard sizes from the Hub), ``context_window`` (the GGUF's own
  ``<arch>.context_length``), its shared knobs, and the F16 KV-cache default
  for 16-bit quants.
- **Stated by the agent, checked against the GGUF header**: whether the quant
  carries built-in MTP layers (``<arch>.nextn_predict_layers``) and which YaRN
  context knob fits its ``general.architecture``. A mismatch is refused, so a
  wrong claim can never reach a catalog file.
- **Refused**: a name any served entry already uses (a user file named like a
  shipped entry would silently replace it), a quant another entry already
  serves, and MTP heads that borrow the target's tensors (``shared-*``), which
  mainline llama.cpp cannot load.

Headers and repo snapshots are cached for the service's lifetime — one engine —
so the agent reading a header and then adding that quant costs one fetch.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from kodo.llms.local import GgufMetadata, ShardResolutionError, detect_shard_group
from kodo.llms.local_registry import (
    MTP_HEAD_MIN_LLAMACPP_VERSION,
    MTP_SIDECARS_FILENAME,
    MTP_SPEC_DECODE_KNOB,
    SHARED_KNOBS,
    LocalLLMEntry,
    MtpSidecar,
    builtin_catalog_entries,
    builtin_mtp_sidecars,
    context_knob_architectures,
    get_local_registry,
    get_mtp_sidecars,
    load_mtp_sidecars_file,
    local_thinking_family,
    mtp_head_knob_id,
    quant_precision_bits,
    user_catalog_dir,
    user_catalog_entry_path,
    write_user_catalog_entry,
    write_user_mtp_sidecars,
)

from ._errors import ModelImportError
from ._hub import HubClient, HuggingFaceHub, RepoFile, RepoSnapshot
from ._search import SEARCH_MIN_QUERY_LENGTH, rank_search_hits

__all__ = ["LocalCatalogService", "format_size_hint", "guess_quant_type"]

_SHARD = re.compile(r"-(?P<index>\d{5})-of-\d{5}\.gguf$", re.IGNORECASE)
_QUANT_TOKEN = re.compile(
    r"(?:^|[-_.])(?P<quant>(?:UD-)?(?:IQ\d_[A-Z0-9_]+|TQ\d_\d|Q\d_K(?:_[A-Z]+)?|Q\d_\d"
    r"|MXFP4(?:_MOE)?|BF16|F16|F32))$",
    re.IGNORECASE,
)
_HEAD_KEYS = frozenset({"id", "repo_id", "filename", "quant_type"})
_ENTRY_KIND = "hardcoded_hf"
#: Hits a search asks the Hub for — enough that the top publishers' repos are
#: still among them when a popular name is shared by hundreds of fine-tunes.
_SEARCH_FETCH = 100
#: Ranked hits a search returns.
_SEARCH_RESULTS = 20


def _tag_value(tags: Sequence[str], prefix: str) -> str:
    """The first ``prefix:<value>`` tag's value, skipping ``prefix:<relation>:<id>``."""
    for tag in tags:
        if tag.startswith(prefix):
            value = tag[len(prefix) :]
            if ":" not in value:
                return value
    return ""


def format_size_hint(size: int) -> str:
    """Render a byte count the way the shipped catalog does (decimal units).

    Args:
        size (int): Bytes.

    Returns:
        str: ``"17.6 GB"``, ``"1.37 GB"``, ``"128 GB"``, ``"905 MB"``.
    """
    gigabytes = size / 1e9
    if gigabytes >= 100:
        return f"{gigabytes:.0f} GB"
    if gigabytes >= 10:
        return f"{gigabytes:.1f} GB"
    if gigabytes >= 1:
        return f"{gigabytes:.2f} GB"
    return f"{size / 1e6:.0f} MB"


def guess_quant_type(path: str) -> str:
    """Read the quant type off a GGUF file name, as quantizers spell it.

    Args:
        path (str): Repo path (``"UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"``).

    Returns:
        str: The trailing quant token (``"UD-Q4_K_XL"``), upper-cased, or
        ``""`` when the name does not end in one.
    """
    stem = _SHARD.sub("", PurePosixPath(path).name)
    stem = re.sub(r"\.gguf$", "", stem, flags=re.IGNORECASE)
    match = _QUANT_TOKEN.search(stem)
    return match.group("quant").upper() if match else ""


def _precision(quant_type: str) -> int | None:
    try:
        return quant_precision_bits(quant_type)
    except ValueError:
        return None


def _is_mtp_head(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return parts[-1].lower().startswith("mtp-") or (len(parts) > 1 and parts[0].lower() == "mtp")


def _is_projector(path: str) -> bool:
    return PurePosixPath(path).name.lower().startswith("mmproj")


def _require_str(fields: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ModelImportError(f"{key!r} must be a string")
    if not allow_empty and not value.strip():
        raise ModelImportError(f"{key!r} must not be empty")
    return value.strip()


def _require_int(fields: Mapping[str, object], key: str) -> int:
    value = fields.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelImportError(f"{key!r} must be a non-negative integer")
    return value


def _require_bool(fields: Mapping[str, object], key: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        raise ModelImportError(f"{key!r} must be true or false")
    return value


class _QuantFile:
    """One quant in a repo: a single GGUF, or the shard set of a split one."""

    __filename: str
    __shards: tuple[str, ...]
    __total_bytes: int

    def __init__(self, filename: str, shards: tuple[str, ...], total_bytes: int) -> None:
        """Describe one quant.

        Args:
            filename (str): The file llama-server is pointed at — the first shard.
            shards (tuple[str, ...]): Every file of the quant, in order.
            total_bytes (int): Their summed size.
        """
        self.__filename = filename
        self.__shards = shards
        self.__total_bytes = total_bytes

    @property
    def filename(self) -> str:
        return self.__filename

    @property
    def shards(self) -> tuple[str, ...]:
        return self.__shards

    @property
    def total_bytes(self) -> int:
        return self.__total_bytes

    def to_dict(self) -> dict[str, object]:
        """Describe the quant for the agent.

        Returns:
            dict[str, object]: ``filename``, ``shards``, ``total_bytes``,
            ``size_hint``, ``quant_type_guess`` and ``precision_bits``.
        """
        quant_type = guess_quant_type(self.__filename)
        return {
            "filename": self.__filename,
            "shards": len(self.__shards),
            "total_bytes": self.__total_bytes,
            "size_hint": format_size_hint(self.__total_bytes),
            "quant_type_guess": quant_type,
            "precision_bits": _precision(quant_type) if quant_type else None,
        }


class _RepoLayout:
    """A repo's GGUF files sorted into quants, MTP heads and vision projectors."""

    __quants: tuple[_QuantFile, ...]
    __heads: tuple[RepoFile, ...]
    __projectors: tuple[RepoFile, ...]
    __incomplete: tuple[str, ...]

    def __init__(self, snapshot: RepoSnapshot) -> None:
        """Sort *snapshot*'s files.

        Args:
            snapshot (RepoSnapshot): The repository.
        """
        sizes = {f.path: f.size for f in snapshot.files}
        paths = [f.path for f in snapshot.files]
        quants: list[_QuantFile] = []
        heads: list[RepoFile] = []
        projectors: list[RepoFile] = []
        incomplete: list[str] = []
        for file in snapshot.files:
            if not file.path.lower().endswith(".gguf"):
                continue
            if _is_projector(file.path):
                projectors.append(file)
                continue
            if _is_mtp_head(file.path):
                heads.append(file)
                continue
            match = _SHARD.search(file.path)
            if match is not None and int(match.group("index")) != 1:
                continue
            try:
                shards = tuple(detect_shard_group(file.path, paths))
            except ShardResolutionError:
                incomplete.append(file.path)
                continue
            quants.append(_QuantFile(file.path, shards, sum(sizes.get(s, 0) for s in shards)))
        self.__quants = tuple(quants)
        self.__heads = tuple(heads)
        self.__projectors = tuple(projectors)
        self.__incomplete = tuple(incomplete)

    @property
    def quants(self) -> tuple[_QuantFile, ...]:
        return self.__quants

    @property
    def heads(self) -> tuple[RepoFile, ...]:
        return self.__heads

    @property
    def projectors(self) -> tuple[RepoFile, ...]:
        return self.__projectors

    @property
    def incomplete(self) -> tuple[str, ...]:
        return self.__incomplete

    def quant(self, filename: str) -> _QuantFile | None:
        """The quant whose first file is *filename*.

        Args:
            filename (str): A repo path.

        Returns:
            _QuantFile | None: The quant, or ``None``.
        """
        return next((q for q in self.__quants if q.filename == filename), None)

    def head(self, filename: str) -> RepoFile | None:
        """The MTP head file at *filename*.

        Args:
            filename (str): A repo path.

        Returns:
            RepoFile | None: The head, or ``None``.
        """
        return next((h for h in self.__heads if h.path == filename), None)

    def describe_miss(self, filename: str) -> str:
        """Explain why *filename* is not a quant of this repo.

        Args:
            filename (str): The path the agent passed.

        Returns:
            str: A sentence naming what the path is instead.
        """
        for quant in self.__quants:
            if filename in quant.shards:
                return (
                    f"{filename} is a later shard of a split GGUF; "
                    f"pass its first shard {quant.filename}"
                )
        if any(h.path == filename for h in self.__heads):
            return f"{filename} is an MTP head, not a quant; add it with set_mtp_heads"
        if any(p.path == filename for p in self.__projectors):
            return f"{filename} is a vision projector (mmproj), not a quant"
        if filename in self.__incomplete:
            return f"{filename} is a split GGUF with shards missing from the repo"
        return f"{filename} is not a GGUF quant in this repo; read_hf_model lists them"


class LocalCatalogService:
    """Reads GGUF repos and writes quants and MTP heads into the user catalog.

    Every public method returns a JSON-serialisable dict and raises
    :class:`ModelImportError` with a message the calling agent can act on.
    """

    __kodo_dir: Path
    __hub: HubClient
    __snapshots: dict[str, RepoSnapshot]
    __headers: dict[tuple[str, str], GgufMetadata]

    def __init__(self, kodo_dir: Path, *, hub: HubClient | None = None) -> None:
        """Bind the service to a ``~/.kodo`` directory.

        Args:
            kodo_dir (Path): User-level ``~/.kodo``; entries are written under
                its ``local_llms/``.
            hub (HubClient | None): Where repos and headers come from; ``None``
                uses the public Hugging Face Hub.
        """
        self.__kodo_dir = kodo_dir
        self.__hub = hub if hub is not None else HuggingFaceHub()
        self.__snapshots = {}
        self.__headers = {}

    async def list_catalog(self) -> dict[str, object]:
        """Describe every catalog family the registry serves, and the context knobs.

        Returns:
            dict[str, object]: ``families`` (one per ``base_llm``: its entries,
            the ``llamacpp_version`` values in use, its thinking family and
            MTP heads), ``context_knobs`` (id, architecture, native context),
            and ``user_catalog_dir``.
        """
        registry = get_local_registry(self.__kodo_dir)
        families: dict[str, dict[str, object]] = {}
        entries_by_family: dict[str, list[dict[str, object]]] = {}
        versions_by_family: dict[str, set[int]] = {}
        for entry in registry.values():
            if entry.kind != _ENTRY_KIND or not entry.base_llm:
                continue
            user_file = user_catalog_entry_path(self.__kodo_dir, entry.name)
            entries_by_family.setdefault(entry.base_llm, []).append(
                {
                    "name": entry.name,
                    "source": "shipped" if user_file is None else "user",
                    "repo_id": entry.repo_id,
                    "filename": entry.filename,
                    "quant_type": entry.quant_type,
                    "size_hint": entry.size_hint,
                    "mtp_supported": entry.mtp_supported,
                }
            )
            versions_by_family.setdefault(entry.base_llm, set()).add(entry.llamacpp_version)
        for base_llm, entries in entries_by_family.items():
            families[base_llm] = {
                "base_llm": base_llm,
                "llamacpp_versions": sorted(versions_by_family[base_llm]),
                "thinking_family": local_thinking_family(base_llm),
                "mtp_heads": [h.id for h in get_mtp_sidecars(self.__kodo_dir, base_llm)],
                "entries": entries,
            }
        return {
            "families": list(families.values()),
            "context_knobs": [
                {"id": knob_id, "architecture": arch, "native_context": native}
                for knob_id, (arch, native) in context_knob_architectures().items()
            ],
            "user_catalog_dir": str(user_catalog_dir(self.__kodo_dir)),
        }

    async def search_repos(self, query: str) -> dict[str, object]:
        """Find GGUF repos for the "add a local LLM" search box.

        Hits are ranked by publisher tier, then downloads
        (:func:`rank_search_hits`). A query shorter than
        :data:`SEARCH_MIN_QUERY_LENGTH` after trimming returns no results
        without asking the Hub.

        Args:
            query (str): What the user has typed so far.

        Returns:
            dict[str, object]: ``query`` (as given) and ``results``: one dict per
            repo with ``repo_id``, ``author``, ``publisher_tier``, ``downloads``,
            ``likes``, ``gated``, ``last_modified``, ``base_model``, ``license``
            and ``in_catalog`` (an entry the registry serves already uses the repo).

        Raises:
            ModelImportError: The Hub could not be searched.
        """
        text = query.strip()
        if len(text) < SEARCH_MIN_QUERY_LENGTH:
            return {"query": query, "results": []}
        hits = await self.__hub.search(text, _SEARCH_FETCH)
        served = {
            entry.repo_id.lower()
            for entry in get_local_registry(self.__kodo_dir).values()
            if entry.repo_id
        }
        return {
            "query": query,
            "results": [
                {
                    "repo_id": ranked.hit.repo_id,
                    "author": ranked.author,
                    "publisher_tier": ranked.tier,
                    "downloads": ranked.hit.downloads,
                    "likes": ranked.hit.likes,
                    "gated": ranked.hit.gated,
                    "last_modified": ranked.hit.last_modified,
                    "base_model": _tag_value(ranked.hit.tags, "base_model:"),
                    "license": _tag_value(ranked.hit.tags, "license:"),
                    "in_catalog": ranked.hit.repo_id.lower() in served,
                }
                for ranked in rank_search_hits(hits, _SEARCH_RESULTS)
            ],
        }

    async def model_info(self, repo_id: str) -> dict[str, object]:
        """Describe one Hugging Face repo: its card, license and GGUF files.

        Args:
            repo_id (str): Hugging Face repository id.

        Returns:
            dict[str, object]: Card metadata, ``readme``, and the GGUF files
            sorted into ``gguf_quants``, ``mtp_head_files`` and ``mmproj_files``.

        Raises:
            ModelImportError: The repo cannot be read.
        """
        snapshot = await self.__snapshot(repo_id)
        layout = _RepoLayout(snapshot)
        return {
            "repo_id": snapshot.repo_id,
            "author": snapshot.author,
            "gated": snapshot.gated,
            "license": snapshot.license,
            "license_name": snapshot.license_name,
            "license_link": snapshot.license_link,
            "base_models": list(snapshot.base_models),
            "tags": list(snapshot.tags),
            "pipeline_tag": snapshot.pipeline_tag,
            "gguf_quants": [q.to_dict() for q in layout.quants],
            "mtp_head_files": [
                {
                    "filename": head.path,
                    "size_hint": format_size_hint(head.size),
                    "quant_type_guess": guess_quant_type(head.path),
                }
                for head in layout.heads
            ],
            "mmproj_files": [p.path for p in layout.projectors],
            "incomplete_split_files": list(layout.incomplete),
            "readme": snapshot.readme,
            "readme_truncated": snapshot.readme_truncated,
        }

    async def gguf_header(self, repo_id: str, filename: str) -> dict[str, object]:
        """Read one GGUF file's header.

        Args:
            repo_id (str): Hugging Face repository id.
            filename (str): The ``.gguf`` file — the first shard of a split one.

        Returns:
            dict[str, object]: :meth:`~kodo.llms.local.GgufMetadata.to_dict`.

        Raises:
            ModelImportError: The header cannot be read.
        """
        return (await self.__header(repo_id, filename)).to_dict()

    async def add_quant(self, fields: Mapping[str, object]) -> dict[str, object]:
        """Write one quant as ``<user catalog>/<base_llm>/<name>.json``.

        Args:
            fields (Mapping[str, object]): ``base_llm``, ``name``, ``repo_id``,
                ``filename``, ``quant_type``, ``description``, ``quant_author``,
                ``llm_author``, ``license_name``, ``license_url``, ``gpu_tip``,
                ``mac_tip``, ``llamacpp_version``, ``min_memory``, ``memory``,
                ``builtin_mtp`` and ``context_knob`` (a knob id, or ``""``/absent
                for none).

        Returns:
            dict[str, object]: The written file's path and the entry's derived
            fields, the family's thinking family and MTP heads.

        Raises:
            ModelImportError: A field is malformed, the name or quant is taken,
                a claim disagrees with the GGUF header, or the file is invalid.
        """
        base_llm = _require_str(fields, "base_llm")
        name = _require_str(fields, "name")
        repo_id = _require_str(fields, "repo_id")
        filename = _require_str(fields, "filename")
        quant_type = _require_str(fields, "quant_type")
        min_memory = _require_int(fields, "min_memory")
        memory = _require_int(fields, "memory")
        builtin_mtp = _require_bool(fields, "builtin_mtp")
        raw_knob = fields.get("context_knob")
        if raw_knob is not None and not isinstance(raw_knob, str):
            raise ModelImportError("'context_knob' must be a knob id string, or empty for none")
        context_knob = (raw_knob or "").strip()
        if memory < min_memory:
            raise ModelImportError(
                f"'memory' ({memory}) must be at least 'min_memory' ({min_memory})"
            )
        bits = _precision(quant_type)
        if bits is None:
            raise ModelImportError(
                f"quant_type {quant_type!r} has no bit width to read (expected e.g. Q4_K_M, "
                "UD-Q6_K_XL, IQ3_XXS, BF16)"
            )

        registry = get_local_registry(self.__kodo_dir)
        self.__refuse_taken(registry, name=name, repo_id=repo_id, filename=filename)
        layout = _RepoLayout(await self.__snapshot(repo_id))
        quant = layout.quant(filename)
        if quant is None:
            raise ModelImportError(layout.describe_miss(filename))
        header = await self.__header(repo_id, filename)
        has_mtp = header.nextn_predict_layers > 0
        if builtin_mtp != has_mtp:
            raise ModelImportError(
                f"builtin_mtp is {str(builtin_mtp).lower()} but {filename}'s header has "
                f"{header.architecture}.nextn_predict_layers = {header.nextn_predict_layers}; "
                f"pass builtin_mtp={str(has_mtp).lower()}"
            )
        if header.context_length <= 0:
            raise ModelImportError(
                f"{filename}'s header states no {header.architecture}.context_length"
            )
        self.__check_context_knob(context_knob, header)

        knobs = [knob.id for knob in SHARED_KNOBS]
        if context_knob:
            knobs.append(context_knob)
        if has_mtp:
            knobs.append(MTP_SPEC_DECODE_KNOB.id)
        body: dict[str, object] = {
            "description": _require_str(fields, "description"),
            "repo_id": repo_id,
            "filename": filename,
            "quant_author": _require_str(fields, "quant_author"),
            "quant_type": quant_type,
            "size_hint": format_size_hint(quant.total_bytes),
            "llm_author": _require_str(fields, "llm_author"),
            "license_name": _require_str(fields, "license_name"),
            "license_url": _require_str(fields, "license_url", allow_empty=True),
            "gpu_tip": _require_str(fields, "gpu_tip", allow_empty=True),
            "mac_tip": _require_str(fields, "mac_tip", allow_empty=True),
            "context_window": header.context_length,
            "llamacpp_version": _require_int(fields, "llamacpp_version"),
            "min_memory": min_memory,
            "memory": memory,
            "mtp_supported": has_mtp,
            "knobs": knobs,
            "knob_defaults": {"kv-cache": "f16"} if bits >= 16 else {},
        }
        try:
            entry, path = write_user_catalog_entry(
                self.__kodo_dir, body, base_llm=base_llm, name=name
            )
        except ValueError as exc:
            raise ModelImportError(str(exc)) from exc
        return {
            "path": str(path),
            "name": entry.name,
            "base_llm": entry.base_llm,
            "size_hint": entry.size_hint,
            "context_window": entry.context_window,
            "mtp_supported": entry.mtp_supported,
            "knobs": [knob.id for knob in entry.knobs],
            "knob_defaults": dict(entry.knob_defaults),
            "thinking_family": local_thinking_family(base_llm),
            "family_mtp_heads": [h.id for h in get_mtp_sidecars(self.__kodo_dir, base_llm)],
        }

    async def set_mtp_heads(self, base_llm: str, heads: Sequence[object]) -> dict[str, object]:
        """Add standalone MTP draft heads to a family's ``mtp_sidecars.json``.

        The family's current heads — its user file if it has one, else the
        shipped list — are kept; a head whose id is already listed for the same
        file is left as is.

        Args:
            base_llm (str): The family; it must already have at least one entry.
            heads (Sequence[object]): ``{id, repo_id, filename, quant_type}`` objects.

        Returns:
            dict[str, object]: The written path (``""`` when every head was
            already listed, and nothing was written), every head the family now
            offers, which ids were added, and the picker knob ids.

        Raises:
            ModelImportError: The family has no entries, a head is malformed,
                not a self-contained MTP head of the family's architecture, or
                collides with a listed head.
        """
        if not isinstance(base_llm, str) or not base_llm.strip():
            raise ModelImportError("'base_llm' must not be empty")
        family = [e for e in get_local_registry(self.__kodo_dir).values() if e.base_llm == base_llm]
        if not family:
            raise ModelImportError(
                f"no catalog entry has base_llm {base_llm!r}; add the family's quants first"
            )
        if not heads:
            raise ModelImportError("'heads' must list at least one head")
        family_arch = (await self.__header(family[0].repo_id, family[0].filename)).architecture

        current = self.__current_heads(base_llm)
        by_id = {head.id: head for head in current}
        added: list[str] = []
        for raw in heads:
            sidecar = await self.__checked_head(raw, family_arch)
            listed = by_id.get(sidecar.id)
            if listed is not None:
                if (listed.repo_id, listed.filename) != (sidecar.repo_id, sidecar.filename):
                    raise ModelImportError(
                        f"head id {sidecar.id!r} already names {listed.repo_id}/{listed.filename} "
                        "in this family; pick another id"
                    )
                continue
            same_file = next(
                (
                    h
                    for h in by_id.values()
                    if (h.repo_id, h.filename) == (sidecar.repo_id, sidecar.filename)
                ),
                None,
            )
            if same_file is not None:
                raise ModelImportError(
                    f"{sidecar.filename} is already listed under head id {same_file.id!r}"
                )
            by_id[sidecar.id] = sidecar
            added.append(sidecar.id)
        path: Path | None = None
        if added:
            # Only when something changed: a user file replaces the shipped list,
            # so rewriting an unchanged one would freeze it against later releases.
            try:
                path = write_user_mtp_sidecars(self.__kodo_dir, base_llm, tuple(by_id.values()))
            except ValueError as exc:
                raise ModelImportError(str(exc)) from exc
        return {
            "path": str(path) if path is not None else "",
            "heads": [
                {"id": h.id, "quant_type": h.quant_type, "size_hint": h.size_hint}
                for h in get_mtp_sidecars(self.__kodo_dir, base_llm)
            ],
            "added": added,
            "picker_knobs": sorted(
                {mtp_head_knob_id(base_llm, builtin=e.mtp_supported) for e in family}
            ),
        }

    async def __snapshot(self, repo_id: str) -> RepoSnapshot:
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise ModelImportError("'repo_id' must not be empty")
        cached = self.__snapshots.get(repo_id)
        if cached is None:
            cached = await self.__hub.snapshot(repo_id)
            self.__snapshots[repo_id] = cached
        return cached

    async def __header(self, repo_id: str, filename: str) -> GgufMetadata:
        if not isinstance(filename, str) or not filename.strip():
            raise ModelImportError("'filename' must not be empty")
        key = (repo_id, filename)
        cached = self.__headers.get(key)
        if cached is None:
            cached = await self.__hub.gguf_header(repo_id, filename)
            self.__headers[key] = cached
        return cached

    @staticmethod
    def __refuse_taken(
        registry: Mapping[str, LocalLLMEntry], *, name: str, repo_id: str, filename: str
    ) -> None:
        if name in registry:
            shipped = any(e.name == name for e in builtin_catalog_entries())
            if shipped:
                raise ModelImportError(
                    f"a shipped catalog entry is named {name!r}; a user file with that name would "
                    "replace it — pick another name"
                )
            raise ModelImportError(f"an entry named {name!r} already exists — pick another name")
        serving = [
            e.name for e in registry.values() if (e.repo_id, e.filename) == (repo_id, filename)
        ]
        if serving:
            raise ModelImportError(
                f"{repo_id}/{filename} is already served by entry {serving[0]!r}; skip this quant"
            )

    @staticmethod
    def __check_context_knob(context_knob: str, header: GgufMetadata) -> None:
        targets = context_knob_architectures()
        matching = sorted(k for k, (arch, _) in targets.items() if arch == header.architecture)
        if not context_knob:
            if matching:
                raise ModelImportError(
                    f"the GGUF's architecture {header.architecture!r} has a context knob; "
                    f"pass context_knob={matching[0]!r}"
                )
            return
        target = targets.get(context_knob)
        if target is None:
            raise ModelImportError(
                f"unknown context knob {context_knob!r}; known: {', '.join(sorted(targets))}"
            )
        if target[0] != header.architecture:
            hint = f"pass {matching[0]!r}" if matching else "pass an empty context_knob"
            raise ModelImportError(
                f"context knob {context_knob!r} targets architecture {target[0]!r} but the GGUF "
                f"is {header.architecture!r} — llama.cpp would ignore it; {hint}"
            )

    def __current_heads(self, base_llm: str) -> tuple[MtpSidecar, ...]:
        user_file = user_catalog_dir(self.__kodo_dir) / base_llm / MTP_SIDECARS_FILENAME
        if not user_file.is_file():
            return builtin_mtp_sidecars().get(base_llm, ())
        try:
            return load_mtp_sidecars_file(user_file)
        except ValueError as exc:
            raise ModelImportError(
                f"the existing {user_file} is invalid ({exc}); fix or delete it first"
            ) from exc

    async def __checked_head(self, raw: object, family_arch: str) -> MtpSidecar:
        if not isinstance(raw, dict):
            raise ModelImportError(
                "each head must be an object {id, repo_id, filename, quant_type}"
            )
        unknown = sorted(str(k) for k in raw if k not in _HEAD_KEYS)
        if unknown:
            raise ModelImportError(f"unknown head key(s) {', '.join(unknown)}")
        head_id = _require_str(raw, "id")
        repo_id = _require_str(raw, "repo_id")
        filename = _require_str(raw, "filename")
        quant_type = _require_str(raw, "quant_type")
        if _precision(quant_type) is None:
            raise ModelImportError(f"head {head_id!r}: quant_type {quant_type!r} has no bit width")
        layout = _RepoLayout(await self.__snapshot(repo_id))
        file = layout.head(filename)
        if file is None:
            raise ModelImportError(
                f"head {head_id!r}: {filename} is not an MTP head file of {repo_id} "
                "(read_hf_model lists them under mtp_head_files)"
            )
        header = await self.__header(repo_id, filename)
        if header.nextn_predict_layers <= 0:
            raise ModelImportError(f"head {head_id!r}: {filename} carries no MTP layers")
        if header.shared_target_tensors:
            raise ModelImportError(
                f"head {head_id!r}: {filename} borrows the target model's tensors "
                f"({header.architecture}.nextn_shared_target_tensors), which mainline llama.cpp "
                "cannot load — skip it"
            )
        if header.architecture != family_arch:
            raise ModelImportError(
                f"head {head_id!r}: {filename} is architecture {header.architecture!r} but the "
                f"family's quants are {family_arch!r}"
            )
        return MtpSidecar(
            id=head_id,
            repo_id=repo_id,
            filename=filename,
            quant_type=quant_type,
            size_hint=format_size_hint(file.size),
            llamacpp_version=MTP_HEAD_MIN_LLAMACPP_VERSION,
        )
