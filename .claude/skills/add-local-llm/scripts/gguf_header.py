#!/usr/bin/env python3
"""Dump the metadata header of a GGUF file on Hugging Face without downloading the weights.

Stdlib only on purpose: it must keep working when kodo's own GGUF reader
(``kodo.llms.local.read_gguf_metadata``) changes or truncates long strings.

Usage:
    gguf_header.py <repo_id> <filename-in-repo> [--template OUT.jinja] [--max-array N]

For a split GGUF pass the first shard (``...-00001-of-0000N.gguf``) — it holds
the metadata. Set HF_TOKEN for gated repos.

Prints every scalar key (strings cut at 300 chars), every array key as
``type[count]`` — printed in full when it is numeric and no longer than
--max-array (default 512), which is what exposes per-layer facts such as
``<arch>.attention.head_count_kv`` — and, with --template, writes the full
``tokenizer.chat_template`` to a file.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import urllib.request

_TYPES = {
    0: ("uint8", "<B", 1),
    1: ("int8", "<b", 1),
    2: ("uint16", "<H", 2),
    3: ("int16", "<h", 2),
    4: ("uint32", "<I", 4),
    5: ("int32", "<i", 4),
    6: ("float32", "<f", 4),
    7: ("bool", "<?", 1),
    10: ("uint64", "<Q", 8),
    11: ("int64", "<q", 8),
    12: ("float64", "<d", 8),
}
_STRING = 8
_ARRAY = 9
_MAX_BYTES = 256 * 1024 * 1024


class _NeedMore(Exception):
    pass


def _fetch(repo: str, filename: str, end: int) -> bytes:
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{end - 1}", "User-Agent": "kodo-skill"})
    token = os.environ.get("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


class _Reader:
    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise _NeedMore
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str, size: int) -> object:
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> str:
        n = int(self.unpack("<Q", 8))  # type: ignore[arg-type]
        return self.take(n).decode("utf-8", "replace")


def _parse(buf: bytes) -> list[tuple[str, int, object]]:
    r = _Reader(buf)
    if r.take(4) != b"GGUF":
        sys.exit("not a GGUF file (bad magic) — is the filename right?")
    version = r.unpack("<I", 4)
    _tensor_count = r.unpack("<Q", 8)
    kv_count = int(r.unpack("<Q", 8))  # type: ignore[arg-type]
    out: list[tuple[str, int, object]] = [("(gguf.version)", 4, version), ("(gguf.tensor_count)", 10, _tensor_count)]
    for _ in range(kv_count):
        key = r.string()
        vtype = int(r.unpack("<I", 4))  # type: ignore[arg-type]
        out.append((key, vtype, _value(r, vtype)))
    return out


def _value(r: _Reader, vtype: int) -> object:
    if vtype == _STRING:
        return r.string()
    if vtype == _ARRAY:
        etype = int(r.unpack("<I", 4))  # type: ignore[arg-type]
        count = int(r.unpack("<Q", 8))  # type: ignore[arg-type]
        return (etype, [_value(r, etype) for _ in range(count)])
    name, fmt, size = _TYPES[vtype]
    return r.unpack(fmt, size)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("filename")
    ap.add_argument("--template", help="write tokenizer.chat_template here")
    ap.add_argument("--max-array", type=int, default=512)
    args = ap.parse_args()

    window = 4 * 1024 * 1024
    while True:
        buf = _fetch(args.repo, args.filename, window)
        try:
            kvs = _parse(buf)
            break
        except _NeedMore:
            if len(buf) < window or window >= _MAX_BYTES:
                sys.exit(f"header did not parse within {len(buf)} bytes")
            window *= 2

    for key, vtype, value in kvs:
        if vtype == _ARRAY:
            etype, items = value  # type: ignore[misc]
            ename = "string" if etype == _STRING else _TYPES.get(etype, ("?",))[0]
            if etype != _STRING and len(items) <= args.max_array:
                print(f"{key} = {ename}[{len(items)}] {items}")
            else:
                print(f"{key} = {ename}[{len(items)}]")
        elif key == "tokenizer.chat_template":
            text = str(value)
            print(f"{key} = <{len(text)} chars>")
            if args.template:
                with open(args.template, "w", encoding="utf-8") as fh:
                    fh.write(text)
                print(f"  (written to {args.template})")
        else:
            text = repr(value) if not isinstance(value, str) else value
            print(f"{key} = {text[:300]}{'…' if len(text) > 300 else ''}")


if __name__ == "__main__":
    main()
