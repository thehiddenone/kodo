#!/usr/bin/env python3
"""Find the first llama.cpp release build (bNNNNN) that contains a PR or commit.

Stdlib only. Usage:
    llamacpp_first_release.py <PR number | commit sha> [...]

For each argument prints the merge commit, its merge date, and the first
``bNNNNN`` release tag that contains it — the number to put in a catalog
entry's ``llamacpp_version``. With several arguments, also prints the
maximum (the build that has *all* of them). Uses GITHUB_TOKEN if set
(unauthenticated GitHub API allows 60 requests/hour, enough for a few PRs).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

_API = "https://api.github.com/repos/ggml-org/llama.cpp"


def _get(path: str) -> object:
    req = urllib.request.Request(_API + path, headers={"User-Agent": "kodo-skill", "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _resolve(arg: str) -> tuple[str, str]:
    if arg.lstrip("#").isdigit():
        pr = _get(f"/pulls/{arg.lstrip('#')}")
        assert isinstance(pr, dict)
        if not pr.get("merged_at"):
            sys.exit(f"PR #{arg} is not merged (state={pr.get('state')}) — look for the PR that superseded it")
        return str(pr["merge_commit_sha"]), str(pr["merged_at"])
    commit = _get(f"/commits/{arg}")
    assert isinstance(commit, dict)
    return str(commit["sha"]), str(commit["commit"]["committer"]["date"])


def _first_release(sha: str, merged_at: str) -> str:
    after: list[tuple[str, str]] = []
    page = 1
    while True:
        rels = _get(f"/releases?per_page=100&page={page}")
        assert isinstance(rels, list)
        if not rels:
            break
        for rel in rels:
            if rel["published_at"] >= merged_at:
                after.append((rel["published_at"], rel["tag_name"]))
        if rels[-1]["published_at"] < merged_at:
            break
        page += 1
    for _, tag in sorted(after):
        cmp = _get(f"/compare/{tag}...{sha}")
        assert isinstance(cmp, dict)
        if cmp.get("status") in ("behind", "identical"):
            return tag
    return "(no release yet)"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    builds: list[int] = []
    for arg in sys.argv[1:]:
        sha, merged_at = _resolve(arg)
        tag = _first_release(sha, merged_at)
        print(f"{arg}: merge {sha[:9]} at {merged_at} -> first release {tag}")
        if tag.startswith("b") and tag[1:].isdigit():
            builds.append(int(tag[1:]))
    if len(builds) > 1:
        print(f"all of them: b{max(builds)}")


if __name__ == "__main__":
    main()
