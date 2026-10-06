"""GGUF header reading (kodo.llms.local.collect_gguf_metadata) over synthetic files.

Every file here is built byte-by-byte by :func:`_gguf`, so the parser is held
to the GGUF spec itself rather than to whatever one real model happens to
contain. The range fetcher is a fake that serves those bytes and records each
request, which is how the growing-window behaviour is observed.
"""

from __future__ import annotations

import struct

import pytest

from kodo.llms.local import MAX_HEADER_BYTES, GgufHeaderError, collect_gguf_metadata

# GGUF value type ids (spec order).
_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STR, _ARR, _U64, _I64, _F64 = range(13)
_FMT = {
    _U8: "<B",
    _I8: "<b",
    _U16: "<H",
    _I16: "<h",
    _U32: "<I",
    _I32: "<i",
    _F32: "<f",
    _BOOL: "<?",
    _U64: "<Q",
    _I64: "<q",
    _F64: "<d",
}


def _str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _value(value_type: int, value: object) -> bytes:
    if value_type == _STR:
        assert isinstance(value, str)
        return _str(value)
    return struct.pack(_FMT[value_type], value)


def _kv(key: str, value_type: int, value: object) -> bytes:
    return _str(key) + struct.pack("<I", value_type) + _value(value_type, value)


def _array(key: str, element_type: int, items: list[object]) -> bytes:
    body = b"".join(_value(element_type, item) for item in items)
    return _str(key) + struct.pack("<IIQ", _ARR, element_type, len(items)) + body


def _gguf(*kvs: bytes, version: int = 3, tensors: int = 7, tail: bytes = b"\0" * 64) -> bytes:
    header = b"GGUF" + struct.pack("<IQQ", version, tensors, len(kvs))
    return header + b"".join(kvs) + tail


class _Fetcher:
    """Serves byte ranges of *data*, recording every request."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[tuple[int, int]] = []

    async def __call__(self, start: int, end: int) -> bytes:
        self.requests.append((start, end))
        return self.data[start : end + 1]


_QWEN_LIKE = (
    _kv("general.architecture", _STR, "qwen35"),
    _kv("general.name", _STR, "Qwen3.8-27B"),
    _kv("general.size_label", _STR, "27B"),
    _kv("general.file_type", _U32, 15),
    _kv("qwen35.block_count", _U32, 65),
    _kv("qwen35.context_length", _U32, 262144),
    _kv("qwen35.nextn_predict_layers", _U32, 1),
    _array("tokenizer.ggml.tokens", _STR, ["a", "bb", "ccc"]),
    _array("qwen35.rope.dimension_sections", _I32, [11, 11, 10, 0]),
    _kv("tokenizer.chat_template", _STR, "{{ messages }}"),
)


async def test_reads_the_facts_a_catalog_entry_needs() -> None:
    meta = await collect_gguf_metadata(_Fetcher(_gguf(*_QWEN_LIKE)), total_size=None)
    assert meta.version == 3
    assert meta.tensor_count == 7
    assert meta.architecture == "qwen35"
    assert meta.name == "Qwen3.8-27B"
    assert meta.size_label == "27B"
    assert meta.file_type == 15
    assert meta.block_count == 65
    assert meta.context_length == 262144
    assert meta.nextn_predict_layers == 1
    assert meta.shared_target_tensors is False
    assert meta.has_chat_template is True


async def test_arrays_are_reported_by_type_and_length_not_content() -> None:
    meta = await collect_gguf_metadata(_Fetcher(_gguf(*_QWEN_LIKE)), total_size=None)
    assert meta.arrays == {
        "tokenizer.ggml.tokens": ("string", 3),
        "qwen35.rope.dimension_sections": ("int32", 4),
    }
    assert "tokenizer.ggml.tokens" not in meta.scalars
    assert meta.to_dict()["arrays"] == {
        "tokenizer.ggml.tokens": {"type": "string", "length": 3},
        "qwen35.rope.dimension_sections": {"type": "int32", "length": 4},
    }


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        (_U8, 200),
        (_I8, -5),
        (_U16, 60000),
        (_I16, -300),
        (_U32, 4_000_000_000),
        (_I32, -2_000_000_000),
        (_F32, 0.5),
        (_BOOL, True),
        (_STR, "text"),
        (_U64, 2**40),
        (_I64, -(2**40)),
        (_F64, 1e-6),
    ],
)
async def test_every_scalar_type_round_trips(value_type: int, value: object) -> None:
    data = _gguf(_kv("general.architecture", _STR, "x"), _kv("x.value", value_type, value))
    meta = await collect_gguf_metadata(_Fetcher(data), total_size=None)
    assert meta.scalars["x.value"] == value


async def test_absent_mtp_and_moe_keys_read_as_zero() -> None:
    data = _gguf(_kv("general.architecture", _STR, "gemma4"), _kv("gemma4.context_length", _U32, 8))
    meta = await collect_gguf_metadata(_Fetcher(data), total_size=None)
    assert meta.nextn_predict_layers == 0
    assert meta.expert_count == 0
    assert meta.split_count == 0
    assert meta.file_type == -1
    assert meta.has_chat_template is False


async def test_a_shared_mtp_head_is_flagged() -> None:
    data = _gguf(
        _kv("general.architecture", _STR, "qwen4exp"),
        _kv("qwen4exp.nextn_shared_target_tensors", _BOOL, True),
        _kv("qwen4exp.nextn_predict_layers", _U32, 1),
        _kv("qwen4exp.expert_count", _U32, 512),
        _kv("qwen4exp.expert_used_count", _U32, 10),
    )
    meta = await collect_gguf_metadata(_Fetcher(data), total_size=None)
    assert meta.shared_target_tensors is True
    assert (meta.expert_count, meta.expert_used_count) == (512, 10)


async def test_long_strings_are_cut() -> None:
    data = _gguf(_kv("general.architecture", _STR, "x"), _kv("x.long", _STR, "y" * 5000))
    meta = await collect_gguf_metadata(_Fetcher(data), total_size=None)
    value = meta.scalars["x.long"]
    assert isinstance(value, str) and value.endswith("…") and len(value) == 2001


async def test_a_header_past_the_first_window_is_fetched_in_growing_contiguous_windows() -> None:
    big_vocab = _array("tokenizer.ggml.tokens", _STR, ["t" * 100] * 60_000)  # ~6.5 MB
    data = _gguf(
        _kv("general.architecture", _STR, "x"), big_vocab, _kv("x.context_length", _U32, 9)
    )
    fetcher = _Fetcher(data)
    meta = await collect_gguf_metadata(fetcher, total_size=None)
    assert meta.context_length == 9
    assert len(fetcher.requests) == 2
    (s1, e1), (s2, e2) = fetcher.requests
    assert s1 == 0
    assert s2 == e1 + 1  # each window continues where the last one ended
    assert e2 - s2 + 1 == 2 * (e1 - s1 + 1)  # and is twice as large


async def test_no_range_past_the_known_file_size_is_requested() -> None:
    data = _gguf(*_QWEN_LIKE)
    fetcher = _Fetcher(data)
    await collect_gguf_metadata(fetcher, total_size=len(data))
    assert fetcher.requests == [(0, len(data) - 1)]


async def test_a_file_ending_inside_its_header_is_refused() -> None:
    data = _gguf(*_QWEN_LIKE)[:40]
    with pytest.raises(GgufHeaderError, match="ends inside"):
        await collect_gguf_metadata(_Fetcher(data), total_size=len(data))


async def test_a_header_larger_than_the_cap_is_refused_without_reading_past_it() -> None:
    # A string whose declared length is beyond the cap: the parser can never finish.
    head = b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", MAX_HEADER_BYTES * 2)
    requests: list[tuple[int, int]] = []

    async def _endless(start: int, end: int) -> bytes:
        requests.append((start, end))
        return (head + b"\0" * (end + 1))[start : end + 1]

    with pytest.raises(GgufHeaderError, match="larger than 64 MB"):
        await collect_gguf_metadata(_endless, total_size=None)
    assert max(end for _, end in requests) == MAX_HEADER_BYTES - 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"GGML" + b"\0" * 60, "missing the GGUF magic"),
        (b"GGUF" + struct.pack("<I", 1) + b"\0" * 60, "unsupported GGUF version 1"),
        (b"GGUF" + struct.pack(">I", 3) + b"\0" * 60, "big-endian"),
    ],
)
async def test_files_that_are_not_a_supported_gguf_are_refused(data: bytes, message: str) -> None:
    with pytest.raises(GgufHeaderError, match=message):
        await collect_gguf_metadata(_Fetcher(data), total_size=len(data))


async def test_an_empty_range_response_is_an_error_not_a_loop() -> None:
    async def _nothing(start: int, end: int) -> bytes:
        return b""

    with pytest.raises(GgufHeaderError, match="no bytes"):
        await collect_gguf_metadata(_nothing, total_size=None)
