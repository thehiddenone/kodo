"""Read a GGUF file's key/value header over HTTP Range requests.

A GGUF file opens with a little-endian header — magic ``GGUF``, a version, the
tensor and key/value counts — followed by the key/value metadata section that
carries everything a catalog entry needs to know about the model: its
``general.architecture``, ``<arch>.context_length``, whether it ships MTP
layers (``<arch>.nextn_predict_layers``), and whether an MTP head borrows the
target model's tensors (``<arch>.nextn_shared_target_tensors``).

The metadata section is small relative to the weights but not tiny: the
tokenizer arrays alone run to ~11 MB for a 248K-token vocabulary. So the bytes
are fetched in growing windows (4 MB, then doubling) until the section parses,
capped at :data:`MAX_HEADER_BYTES` so a malformed file can never turn into a
full download. The parser itself is pure — :func:`collect_gguf_metadata` takes
any range fetcher — so it is testable without a network.

Spec: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md. Only versions
2 and 3 (64-bit counts and lengths) are read; version 1 predates every model
llama.cpp still loads, and big-endian GGUFs are refused.
"""

from __future__ import annotations

import asyncio
import ssl
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
import certifi

from ._hf import resolve_file
from ._types import GgufHeaderError

__all__ = [
    "MAX_HEADER_BYTES",
    "GgufMetadata",
    "RangeFetcher",
    "collect_gguf_metadata",
    "read_gguf_metadata",
]

#: Hard cap on the bytes fetched to parse one header.
MAX_HEADER_BYTES = 64 * 1024 * 1024

_INITIAL_WINDOW = 4 * 1024 * 1024
_MAGIC = b"GGUF"
_SUPPORTED_VERSIONS = (2, 3)
#: Longest string value reported back verbatim; longer ones are cut with "…".
_MAX_STRING_CHARS = 2000
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=30)
_USER_AGENT = "kodo-model-import/1.0 (github.com/thehiddenone/kodo)"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

#: ``(start, end_inclusive) -> bytes`` — fetch one byte range of the file.
RangeFetcher = Callable[[int, int], Awaitable[bytes]]

# GGUF value type ids → (struct format, byte width) for the fixed-width ones.
_FIXED: dict[int, tuple[str, int]] = {
    0: ("<B", 1),  # UINT8
    1: ("<b", 1),  # INT8
    2: ("<H", 2),  # UINT16
    3: ("<h", 2),  # INT16
    4: ("<I", 4),  # UINT32
    5: ("<i", 4),  # INT32
    6: ("<f", 4),  # FLOAT32
    7: ("<?", 1),  # BOOL
    10: ("<Q", 8),  # UINT64
    11: ("<q", 8),  # INT64
    12: ("<d", 8),  # FLOAT64
}
_STRING = 8
_ARRAY = 9
_TYPE_NAMES = {
    0: "uint8",
    1: "int8",
    2: "uint16",
    3: "int16",
    4: "uint32",
    5: "int32",
    6: "float32",
    7: "bool",
    8: "string",
    9: "array",
    10: "uint64",
    11: "int64",
    12: "float64",
}


class _NeedMoreBytes(Exception):
    """The buffer ends before the metadata section does."""


@dataclass(frozen=True)
class GgufMetadata:
    """The facts a GGUF header states about its model.

    Attributes:
        version: GGUF format version (2 or 3).
        tensor_count: Tensors stored in *this file* — ``0`` for the metadata-only
            first shard of a split GGUF, a few dozen for a standalone MTP head.
        architecture: ``general.architecture`` (``"qwen35"``), ``""`` if absent.
        name: ``general.name``, ``""`` if absent.
        size_label: ``general.size_label`` (``"27B"``), ``""`` if absent.
        file_type: ``general.file_type`` (llama.cpp's ``LLAMA_FTYPE_*``), ``-1`` if absent.
        block_count: ``<arch>.block_count``, ``0`` if absent.
        context_length: ``<arch>.context_length`` — the native context window,
            ``0`` if absent.
        nextn_predict_layers: ``<arch>.nextn_predict_layers`` — MTP layers the
            file carries, ``0`` when the key is absent (no MTP).
        shared_target_tensors: Whether ``<arch>.nextn_shared_target_tensors`` is
            set — an MTP head that borrows the target model's embeddings and
            output, which mainline llama.cpp cannot load.
        expert_count: ``<arch>.expert_count``, ``0`` for a dense model.
        expert_used_count: ``<arch>.expert_used_count``, ``0`` for a dense model.
        split_count: ``split.count`` — shards in a split GGUF, ``0`` if unsplit.
        has_chat_template: Whether ``tokenizer.chat_template`` is present.
        scalars: Every non-array key's value; strings longer than 2000
            characters are cut and end in ``"…"``.
        arrays: Every array key → ``(element type name, element count)``.
    """

    version: int
    tensor_count: int
    architecture: str
    name: str
    size_label: str
    file_type: int
    block_count: int
    context_length: int
    nextn_predict_layers: int
    shared_target_tensors: bool
    expert_count: int
    expert_used_count: int
    split_count: int
    has_chat_template: bool
    scalars: dict[str, object]
    arrays: dict[str, tuple[str, int]]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of the metadata.

        Returns:
            dict[str, object]: Every field; ``arrays`` as ``{key: {type, length}}``.
        """
        return {
            "version": self.version,
            "tensor_count": self.tensor_count,
            "architecture": self.architecture,
            "name": self.name,
            "size_label": self.size_label,
            "file_type": self.file_type,
            "block_count": self.block_count,
            "context_length": self.context_length,
            "nextn_predict_layers": self.nextn_predict_layers,
            "shared_target_tensors": self.shared_target_tensors,
            "expert_count": self.expert_count,
            "expert_used_count": self.expert_used_count,
            "split_count": self.split_count,
            "has_chat_template": self.has_chat_template,
            "scalars": dict(self.scalars),
            "arrays": {k: {"type": t, "length": n} for k, (t, n) in self.arrays.items()},
        }


class _Cursor:
    """Sequential little-endian reader over a byte buffer that may end early."""

    __buf: bytes
    __pos: int

    def __init__(self, buf: bytes) -> None:
        """Start reading *buf* at offset 0.

        Args:
            buf (bytes): The bytes fetched so far, from the start of the file.
        """
        self.__buf = buf
        self.__pos = 0

    def take(self, size: int) -> bytes:
        """Consume *size* bytes.

        Args:
            size (int): Byte count.

        Returns:
            bytes: The consumed bytes.

        Raises:
            _NeedMoreBytes: The buffer ends first.
        """
        end = self.__pos + size
        if end > len(self.__buf):
            raise _NeedMoreBytes
        chunk = self.__buf[self.__pos : end]
        self.__pos = end
        return chunk

    def skip(self, size: int) -> None:
        """Consume *size* bytes without copying them.

        Args:
            size (int): Byte count.

        Raises:
            _NeedMoreBytes: The buffer ends first.
        """
        end = self.__pos + size
        if end > len(self.__buf):
            raise _NeedMoreBytes
        self.__pos = end

    def fixed(self, fmt: str, size: int) -> object:
        """Consume one fixed-width value.

        Args:
            fmt (str): ``struct`` format, little-endian.
            size (int): Its width in bytes.

        Returns:
            object: The decoded value.
        """
        value: object = struct.unpack(fmt, self.take(size))[0]
        return value

    def u32(self) -> int:
        """Consume a ``uint32``.

        Returns:
            int: The value.
        """
        return int(struct.unpack("<I", self.take(4))[0])

    def u64(self) -> int:
        """Consume a ``uint64``.

        Returns:
            int: The value.
        """
        return int(struct.unpack("<Q", self.take(8))[0])

    def string(self) -> str:
        """Consume a GGUF string (``uint64`` length + UTF-8 bytes).

        Returns:
            str: The decoded string; invalid UTF-8 is replaced, not raised.
        """
        return self.take(self.u64()).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        """Consume a GGUF string without decoding it."""
        self.skip(self.u64())


def _read_value(cursor: _Cursor, value_type: int) -> object:
    if value_type == _STRING:
        text = cursor.string()
        return text if len(text) <= _MAX_STRING_CHARS else text[:_MAX_STRING_CHARS] + "…"
    fixed = _FIXED.get(value_type)
    if fixed is None:
        raise GgufHeaderError(f"unknown GGUF value type {value_type}")
    return cursor.fixed(*fixed)


def _skip_array(cursor: _Cursor) -> tuple[str, int]:
    element_type = cursor.u32()
    count = cursor.u64()
    if element_type == _STRING:
        for _ in range(count):
            cursor.skip_string()
    elif element_type == _ARRAY:
        raise GgufHeaderError("nested GGUF arrays are not supported")
    else:
        fixed = _FIXED.get(element_type)
        if fixed is None:
            raise GgufHeaderError(f"unknown GGUF array element type {element_type}")
        cursor.skip(fixed[1] * count)
    return _TYPE_NAMES[element_type], count


def _int_key(scalars: dict[str, object], key: str, default: int) -> int:
    value = scalars.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _str_key(scalars: dict[str, object], key: str) -> str:
    value = scalars.get(key)
    return value if isinstance(value, str) else ""


def _parse(buf: bytes) -> GgufMetadata:
    cursor = _Cursor(buf)
    if cursor.take(4) != _MAGIC:
        raise GgufHeaderError("not a GGUF file (missing the GGUF magic)")
    version = cursor.u32()
    if version > 0xFFFF:
        raise GgufHeaderError("big-endian GGUF files are not supported")
    if version not in _SUPPORTED_VERSIONS:
        raise GgufHeaderError(f"unsupported GGUF version {version}")
    tensor_count = cursor.u64()
    kv_count = cursor.u64()

    scalars: dict[str, object] = {}
    arrays: dict[str, tuple[str, int]] = {}
    for _ in range(kv_count):
        key = cursor.string()
        value_type = cursor.u32()
        if value_type == _ARRAY:
            arrays[key] = _skip_array(cursor)
        else:
            scalars[key] = _read_value(cursor, value_type)

    arch = _str_key(scalars, "general.architecture")
    return GgufMetadata(
        version=version,
        tensor_count=tensor_count,
        architecture=arch,
        name=_str_key(scalars, "general.name"),
        size_label=_str_key(scalars, "general.size_label"),
        file_type=_int_key(scalars, "general.file_type", -1),
        block_count=_int_key(scalars, f"{arch}.block_count", 0),
        context_length=_int_key(scalars, f"{arch}.context_length", 0),
        nextn_predict_layers=_int_key(scalars, f"{arch}.nextn_predict_layers", 0),
        shared_target_tensors=scalars.get(f"{arch}.nextn_shared_target_tensors") is True,
        expert_count=_int_key(scalars, f"{arch}.expert_count", 0),
        expert_used_count=_int_key(scalars, f"{arch}.expert_used_count", 0),
        split_count=_int_key(scalars, "split.count", 0),
        has_chat_template="tokenizer.chat_template" in scalars,
        scalars=scalars,
        arrays=arrays,
    )


async def collect_gguf_metadata(fetch: RangeFetcher, *, total_size: int | None) -> GgufMetadata:
    """Fetch growing windows of a GGUF file until its metadata section parses.

    Args:
        fetch (RangeFetcher): Returns the bytes of one inclusive range of the file.
        total_size (int | None): The file's size, when known — no range past it
            is requested.

    Returns:
        GgufMetadata: The parsed header.

    Raises:
        GgufHeaderError: The file is not a supported GGUF, ends inside its
            header, or its header exceeds :data:`MAX_HEADER_BYTES`.
    """
    limit = MAX_HEADER_BYTES if total_size is None else min(MAX_HEADER_BYTES, total_size)
    buf = bytearray()
    window = _INITIAL_WINDOW
    while True:
        try:
            return _parse(bytes(buf))
        except _NeedMoreBytes:
            pass
        if len(buf) >= limit:
            if limit == MAX_HEADER_BYTES:
                raise GgufHeaderError(
                    f"the GGUF header is larger than {MAX_HEADER_BYTES // (1024 * 1024)} MB"
                )
            raise GgufHeaderError("the file ends inside its GGUF header")
        end = min(len(buf) + window, limit) - 1
        chunk = await fetch(len(buf), end)
        if not chunk:
            raise GgufHeaderError("the server returned no bytes for a header range")
        buf += chunk
        window *= 2


async def read_gguf_metadata(
    repo_id: str, filename: str, *, token: str | None = None
) -> GgufMetadata:
    """Read the header of one GGUF file in a Hugging Face repo.

    Only the header is transferred (typically 1-16 MB), never the weights. For
    a split GGUF pass the first shard — it is the one that carries the metadata.

    Args:
        repo_id (str): Hugging Face repository id.
        filename (str): Path of the ``.gguf`` file within the repo.
        token (str | None): HF access token for gated repos; ``None`` reads
            anonymously (``huggingface_hub`` still honours ``HF_TOKEN``).

    Returns:
        GgufMetadata: The parsed header.

    Raises:
        ShardResolutionError: The repo or file does not exist, or is gated.
        GgufHeaderError: The transfer failed or the file is not a supported GGUF.
    """
    resolved = await asyncio.to_thread(resolve_file, repo_id, filename, token=token)
    try:
        async with aiohttp.ClientSession(
            timeout=_REQUEST_TIMEOUT, headers={"User-Agent": _USER_AGENT}
        ) as http:

            async def fetch(start: int, end: int) -> bytes:
                headers = {**resolved.headers, "Range": f"bytes={start}-{end}"}
                async with http.get(resolved.url, headers=headers, ssl=_SSL_CONTEXT) as resp:
                    if resp.status == 206:
                        return await resp.read()
                    if resp.status == 200:
                        # Range ignored: stream from the start, keep only [start, end].
                        data = bytearray()
                        while len(data) <= end:
                            part = await resp.content.read(min(1024 * 1024, end + 1 - len(data)))
                            if not part:
                                break
                            data += part
                        return bytes(data[start:])
                    raise GgufHeaderError(
                        f"HTTP {resp.status} while reading the header of {repo_id}/{filename}"
                    )

            return await collect_gguf_metadata(fetch, total_size=resolved.size)
    except aiohttp.ClientError as exc:
        raise GgufHeaderError(f"could not read the header of {repo_id}/{filename}: {exc}") from exc
