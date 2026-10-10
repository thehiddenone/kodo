#!/usr/bin/env python3
"""List the quants of a GGUF repo on Hugging Face, with shard-summed sizes.

Stdlib only. Usage:
    hf_quants.py <gguf_repo_id>

Prints the repo's card metadata (license, base_model, quantized_by, gated),
the base model's (license, tags, gated), then one row per quant:
decimal-GB size as the HF file listing shows it (the catalog's ``size_hint``
convention), shard count, and the filename to put in a catalog entry (the
first shard for a split GGUF). ``mmproj-*`` projectors, imatrix files and
standalone MTP heads are listed separately — they are never quants.
Set HF_TOKEN for gated repos.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

_SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "kodo-skill"})
    token = os.environ.get("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _card(repo: str) -> dict[str, object]:
    info = _get(f"https://huggingface.co/api/models/{repo}")
    assert isinstance(info, dict)
    card = info.get("cardData") or {}
    return {
        "id": info.get("id"),
        "gated": info.get("gated"),
        "license": card.get("license") if isinstance(card, dict) else None,
        "base_model": card.get("base_model") if isinstance(card, dict) else None,
        "quantized_by": card.get("quantized_by") if isinstance(card, dict) else None,
        "tags": info.get("tags"),
        "createdAt": info.get("createdAt"),
    }


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    repo = sys.argv[1]
    card = _card(repo)
    print("GGUF repo:", json.dumps(card, indent=1))
    base = card.get("base_model")
    for b in base if isinstance(base, list) else [base] if base else []:
        try:
            print("base model:", json.dumps(_card(str(b)), indent=1))
        except Exception as exc:  # noqa: BLE001 — informational only
            print(f"base model {b}: {exc}")

    tree = _get(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true")
    assert isinstance(tree, list)
    sizes: dict[str, int] = defaultdict(int)
    shards: dict[str, list[str]] = defaultdict(list)
    other: list[str] = []
    for item in tree:
        path = item.get("path", "")
        if item.get("type") != "file" or not path.endswith(".gguf"):
            continue
        base_name = path.rsplit("/", 1)[-1].lower()
        if base_name.startswith("mmproj") or "imatrix" in base_name or path.lower().startswith("mtp/"):
            other.append(f"{path}  {item.get('size', 0) / 1e9:.2f} GB")
            continue
        key = _SHARD.sub("", path)
        sizes[key] += int(item.get("size", 0))
        shards[key].append(path)

    print(f"\n{'quant key':70} {'GB':>8} shards  filename")
    for key in sorted(sizes, key=lambda k: -sizes[k]):
        files = sorted(shards[key])
        print(f"{key:70} {sizes[key] / 1e9:8.2f} {len(files):6}  {files[0]}")
    if other:
        print("\nnot quants (projectors / imatrix / MTP heads):")
        for line in other:
            print("  " + line)


if __name__ == "__main__":
    main()
