# Checkpoints: shadow mirror, stateful undo/redo/rollback/roll-forward

This document is the canonical reference for the **generic checkpoint
system**, used unconditionally in **both workflow modes**, that backs the
chat UI's per-tool-call "undo this change" / "re-do this change" link and
"Rollback to this state" / "Roll forward to this state" box. It covers the
shadow-git mirror engine, the persisted stateful checkpoint model layered on
top of it, the exact git mechanics of all four operations, the dirty-work-tree
safety flow, and the wire protocol.

> **Formerly Guided mode ran a second, separate mirror; it is gone.** A
> bespoke artifact-promotion mirror (`kodo.workspace.MirrorRepo` +
> `_promoter.Promoter` + `_checkpoints.CheckpointManager`) used to back the
> Guide's `rollback` tool, walled off from the mechanism documented here
> specifically to avoid colliding on the same `<root>/.kodo/checkpoints` path
> with an incompatible git layout — that was the entire reason this mirror
> used to be gated to Problem-Solver sessions only. `kodo.workspace` is
> deleted outright (STATE_AND_LIFECYCLE.md §1.1/§8). The Guide's `rollback`
> tool now delegates straight to `RootMirrorManager.rollback` — the same
> primitive backing the chat UI's "Rollback to this state" control — and
> additionally resets the conversation (the engine's `_run_rollback`,
> STATE_AND_LIFECYCLE.md §8.3). There is exactly **one** shadow-git mirror per
> root, regardless of which workflow mode touched it.

## 1. The shadow-git mirror (`kodo.mirror.ShadowMirror`)

Every root the agent may touch — a Problem-Solving-mode workspace folder, or
the single bound project in Guided mode — gets its own git repository whose
**git directory** lives apart from its **work tree**:

```
<root>/.kodo/checkpoints/.git    — the mirror's git metadata
<root>/...                        — the work tree IS the real project files
```

Every git invocation runs with `GIT_DIR`/`GIT_WORK_TREE` set in the
environment, so there is never a duplicated working copy and never a `.git`
inside the tracked tree. This is the whole trick: the mirror versions the
project *in place*.

- **Lazy creation.** A root's mirror doesn't exist until the first
  file-mutating tool call touches it (`RootMirrorManager._ensure`). At that
  point `<root>/.kodo/` and a minimal `kodo.md` marker are scaffolded
  (`ProjectLayout.scaffold_kodo_dir`), the mirror is `git init`'d, and
  `info/exclude` is seeded with `_KODO_EXCLUDES` (`.kodo/`, `.git/`,
  `node_modules/`, `.venv/`, `__pycache__/`, build/cache dirs, ...) — on top
  of whatever `.gitignore` the project itself already has.
- **Baseline commit.** `init()` immediately commits whatever already exists
  under the root as `"init: kodo mirror baseline"`. This matters: it means
  undoing the very first tool-call commit restores files to their real
  pre-Kōdo state, not to emptiness.
- **One commit per tool call.** After every `filesystem`/`edit_file`/
  `create_file`/`create_directory`/`run_command` call that might have mutated files (`command_may_mutate`'s
  conservative default-to-mutating heuristic), the engine calls
  `mirror.commit(label)`: `git add -A` + commit, or a no-op (returns the
  unchanged HEAD) if nothing actually changed.
- **Never `$HOME` or `/`.** `RootMirrorManager._ensure` refuses outright
  (`UnsafeCheckpointRootError`, before any git command runs) if the resolved
  root is the user's home directory or a filesystem root — a hard,
  unconditional guard, independent of how the root got proposed. This closed
  a real incident: `SessionWorkspace.physical_root` used to default to
  `Path.home()` whenever the client hadn't pushed `workspace.folders` yet, and
  `EngineCore._root_paths()` used that default as a fallback "root" to keep
  `get_root_paths` non-empty — so a mutating tool call in the gap between "no
  workspace open" and the extension's next `workspace.folders` push could
  seed a mirror rooted at `$HOME` and run `git add -A` over the user's entire
  home directory. Both halves of the fix now hold: `physical_root` is `Path |
  None` with no default (`_root_paths()` returns `()` when nothing is known —
  matching `_has_workspace()`, which already refused to fall back this way),
  *and* the guard above stops a bad root from ever reaching git even if some
  future bug reintroduces one.

## 2. The stateful model on top (`kodo.runtime._checkpoints`)

The mirror alone only gives you a git history. The UI needs to know, *right
now*, for each checkpoint: is it ahead of or behind where the work tree
currently is, and has it been undone? That bookkeeping is
`CheckpointState`, persisted as JSON at:

```
<root>/.kodo/checkpoints/state.json
```

— a sibling of the mirror's own `.git`, **outside** the tracked work tree
(never versioned, never touched by `git add -A`).

```json
{
  "current_index": 2,
  "entries": [
    {"sha": "...", "parent": "...", "label": "create a", "kind": "tool_call", "undone": false, "ts": "..."},
    {"sha": "...", "parent": "...", "label": "edit a",   "kind": "tool_call", "undone": false, "ts": "..."},
    {"sha": "...", "parent": "...", "label": "undo ...", "kind": "undo",      "undone": false, "ts": "..."}
  ]
}
```

### Why a flat list, not a git-ancestry walk

`entries` is a **flat, append-only, chronological** list. `current_index` is
just an index into it — not a literal walk of git's commit graph (no
`merge-base`/ancestor reasoning anywhere). Two rules make this work:

1. **New tool-call checkpoints always append at the end** and advance
   `current_index` to that new last entry — *even if `current_index` wasn't
   already at the end* (i.e. even right after a rollback). The entries that
   were "in the future" relative to the old `current_index` stay in the list
   at their old positions forever, now describing an **abandoned branch**:
   in git they're preserved on a `rollback_<ts>` side branch (§3), but in the
   UI's flat-list model they simply continue to show "Roll forward to this
   state" — even though strictly speaking they're now a sibling history, not
   a true descendant of the new tip. This is a deliberate simplification:
   the flat list is a UI convenience over the chat transcript's chronological
   order, and the git side is allowed to diverge underneath it. (If you do
   roll forward into one of those old entries, the *new* tip then gets
   preserved the same way — branch preservation is symmetric, see §3.)
2. **Rollback/roll-forward only move `current_index`** to an *existing*
   entry — no new entry is appended, because the git side is a ref move, not
   a commit (§3).

### Undo/redo: a per-entry toggle, still append-only commits

`undo`/`redo` did **not** change shape from before this feature — they still
work exactly like `ShadowMirror.undo` always did: restore only the files a
specific commit touched, recorded as a *new forward commit* (append-only, no
branches, no risk of a detached HEAD). What's new is the bookkeeping:

- `undo(sha)`: restores `sha`'s touched files to their state at `sha^`
  (files `sha` created are deleted). Appends a new `kind="undo"` entry and
  flips the **original** entry's `undone` → `True`. That flag — not the new
  entry — is what the UI reads to swap that original entry's link from
  "undo this change" to "re-do this change".
- `redo(sha)`: the mirror image — restores `sha`'s touched files to their
  state **at `sha` itself** (`ShadowMirror.redo`, new in this feature: same
  shape as `undo` but diffed against `sha` instead of `sha^`). Appends a
  `kind="redo"` entry, flips `undone` back to `False`.

Both are **disabled in the UI** (the link is hidden, not just greyed out)
once `current_index` has moved behind that entry — i.e. once a rollback has
made that entry's files no longer the ones actually checked out. Undoing or
redoing a change you're not currently sitting on doesn't make sense; only
"Roll forward to this state" applies there.

## 3. Rollback / roll-forward: real ref moves, never a detached HEAD

This is the one piece of git mechanics that's genuinely new (everything
above already existed). `ShadowMirror.rollback(sha)`:

```python
tip = await self.head_sha()
if sha == tip:
    return tip                                  # no-op
await self.__create_rollback_branch(tip)        # preserve the orphaned tip
await self.__git("reset", "--hard", sha)        # repoint the current branch
return await self.head_sha()
```

- **One primitive, two directions.** "Rollback" (sha behind the tip) and
  "roll forward" (sha ahead, or on a diverged branch) are the *same*
  operation. `RootMirrorManager.roll_forward` is a one-line wrapper around
  `rollback` — the only difference is which way `current_index` moves, and
  that's derived automatically from where `sha` sits in the persisted list.
- **Never detached.** The mirror stays on its one named branch (whatever
  `git init` called it — never hardcoded as `main` vs `master`) for the
  entire operation; only `git reset --hard` moves where that branch points.
  `ShadowMirror.branch_name()` (`git symbolic-ref --short HEAD`) is the
  proof: that command *raises* if HEAD is detached, so successfully reading
  a branch name before and after a rollback demonstrates it never was.
- **Orphan preservation.** Before the reset, if the current tip isn't
  already `sha`, it's branched off as `rollback_<unix-ts>` (collision-safe:
  `-2`, `-3`, ... suffix on a same-second clash). That branch is never
  deleted automatically — it's the thing that keeps the "rolled-back-past"
  commits reachable (not garbage) even though the main branch no longer
  points through them. Two rollbacks/roll-forwards in the same session can
  leave several such branches lying around; that's expected and harmless.

### Worked example

```
master tip: c3, ancestry c1─c2─c3      flat list: [c1, c2, c3]   current_index=2

rollback(c1):
  branch rollback_<ts1> created at c3        (preserves the orphaned tip)
  master reset --hard c1  →  master tip: c1, ancestry c1
  current_index = entries.index(c1) = 0

new tool call c4 (parent=c1):
  master tip: c4, ancestry c1─c4             (diverged from rollback_<ts1>)
  flat list: [c1, c2, c3, c4]                (c4 appended at the end)
  current_index = 3                          (c2, c3 still fully intact —
                                               reachable via rollback_<ts1>,
                                               just no longer via master)

roll_forward(c2):
  branch rollback_<ts2> created at c4        (preserves it before moving away)
  master reset --hard c2  →  master tip: c2, ancestry c1─c2
  current_index = entries.index(c2) = 1
```

The flat list never needs to reason about *why* c2 is reachable — only that
it's recorded in the list and the `git reset --hard c2` succeeded.

## 4. The dirty-work-tree safety check

Edits made to the work tree **without** going through a Kodo tool call (e.g.
typed directly in the editor) never get auto-committed to the mirror, so
they show up as uncommitted/untracked changes from the mirror's point of
view. Every undo/redo/rollback/roll-forward checks for this **before**
touching anything:

- `ShadowMirror.is_dirty()` — `git status --porcelain` non-empty. Coarse and
  deliberately conservative (whole-tree, not just the paths the operation
  would touch) — same philosophy as `command_may_mutate`'s default-to-true:
  a false positive just costs an extra confirmation, a false negative could
  silently lose work.
- If dirty and no `resolution` was given, `RootMirrorManager` raises
  `MirrorDirtyError` rather than mutating anything.
- The caller resolves it with one of:
  - `"discard"` — proceed without stashing. For rollback/roll-forward,
    `git reset --hard` overwrites tracked changes; **untracked** files are
    left alone either way (git never deletes untracked files on a reset), so
    "discard" never destroys data outside git's own tracking.
  - `"stash"` — `git stash push -u` (includes untracked) before the
    operation, `git stash pop` after. If the tree was clean, `stash_push`
    is a no-op (returns `False`, skips the pop too) — no spurious round-trip.

## 5. Wire protocol

`kodo.transport._messages`:

| Constant | Value | Direction |
|---|---|---|
| `MSG_CHECKPOINT_UNDO` | `checkpoint.undo` | client → server |
| `MSG_CHECKPOINT_REDO` | `checkpoint.redo` | client → server |
| `MSG_CHECKPOINT_ROLLBACK` | `checkpoint.rollback` | client → server |
| `MSG_CHECKPOINT_ROLL_FORWARD` | `checkpoint.roll_forward` | client → server |
| `MSG_CHECKPOINT_LIST` | `checkpoint.list` | client → server |
| `EVT_CHECKPOINT_STATE` | `checkpoint.state` | server → client (push) |

All four mutating requests carry `{root, sha, resolution?}`. The server
replies either:

- `checkpoint.<verb>.done` — `{root, sha, current_index, entries: [{sha, undone}]}`
- `checkpoint.<verb>.needs_confirmation` — `{root, sha}` (dirty tree, no
  `resolution` given; the client is expected to ask the user and resubmit
  the *same* request with `resolution` set)

After any successful mutation, the engine **also** pushes
`EVT_CHECKPOINT_STATE` (`{root, current_index, entries: [{sha, undone}]}`)
via the session's `SessionChannel` — independent of the request/response,
because one action can change *every other* checkpoint button's eligible
state for that root, not just the one acted on.

`checkpoint.list` (`handle_checkpoint_list`) returns the same shape and is
used purely for hydration (a UI that wants to (re)fetch a root's full state
without performing a mutation).

### Where `index`/`undone`/`current_index` ride into the chat feed

Per-tool-call checkpoint data travels two ways:

1. **Live**: `EVT_AGENT_TOOL_CALL_DETAIL`'s `checkpoint` field
   (`_finalize_tool_result`) — `{root, sha, parent, index, undone,
   current_index}`, looked up from the just-updated `CheckpointState`
   immediately after the commit.
2. **History replay** (session resume / window reconnect): `checkpoint_sha`
   and `checkpoint_root` are persisted as declared (optional) output-schema
   fields on the five mutating tools (`filesystem`, `edit_file`, `create_file`,
   `create_directory`, `run_command` — see their `output_schema`s) and injected into the
   LLM-visible tool result at `_finalize_tool_result`. `history_entries()` /
   `HistoryProjector._message_to_entries()` reconstruct the same `{root, sha, parent, index,
   undone, current_index}` shape from those two fields plus a
   per-`history_entries()`-call cache of each touched root's
   `CheckpointState` (`HistoryProjector._checkpoint_detail`, loaded at most once per root).
   Before this feature, `checkpoint_sha` was persisted but `checkpoint_root`
   was not, and `HistoryProjector._message_to_entries` never reconstructed a `checkpoint`
   field at all — **undo/rollback controls silently vanished after every
   reload.** Both gaps are closed by this change.

## 6. WebView-side state propagation

`CheckpointData` (`webview/types.ts`) carries `root`, `sha`, `parent`,
`index`, `currentIndex`, `undone` — `index`/`currentIndex` denormalized onto
*every* tool-call entry sharing that `root`, kept in sync by the
`checkpoint_state` reducer case (`reducer.ts`): on
`EVT_CHECKPOINT_STATE`, it walks `state.session` and, for every `tool_call`
entry whose `checkpoint.root` matches, looks its `sha` up in the pushed
`entries` array and refreshes `index`/`currentIndex`/`undone` in one pass.

`SessionEntryView.tsx` reads only those denormalized fields to decide what
to render — no global lookup needed:

- `UndoChangeLink`: hidden when `index > currentIndex`; otherwise toggles
  `↺ undo this change` ↔ `↻ re-do this change` on `undone`, posting
  `checkpoint_undo`/`checkpoint_redo`.
- `RollbackBox`: hidden when `index === currentIndex` (this entry already
  *is* the current state — nothing to do); otherwise `⎌ Rollback to this
  state` (`index < currentIndex`) or a vertically-mirrored `⎌` (CSS
  `transform: scaleX(-1)` — Unicode has no pre-mirrored glyph) "Roll forward
  to this state" (`index > currentIndex`), posting
  `checkpoint_rollback`/`checkpoint_roll_forward`.

`session-controller.ts` is the only place that knows about the
dirty-tree-confirmation dance: on a `checkpoint.*.needs_confirmation` reply
it shows a native VS Code modal (`_confirmCheckpointDirtyTree`, same pattern
as session deletion's `_confirmAndDelete`) offering **Stash & Continue** /
**Discard & Continue**, then resubmits the original request type (derived
by stripping `.needs_confirmation` off the reply's `type`) with
`resolution` set. The webview itself never sees this round-trip — it just
fires the plain request and either gets a `checkpoint_state` update or
nothing happens until the modal resolves.

## 7. Tests

- `test/test_shadow_mirror.py` — mirror-level: init/baseline/excludes,
  commit no-op stability, undo/redo file-level mechanics, the
  branch-preserving rollback (`test_rollback_moves_branch_without_detaching_head`),
  no-op rollback, dirty/stash round-trip.
- `test/test_checkpoint_state.py` — `CheckpointState`/`CheckpointEntry` JSON
  round-trip, `RootMirrorManager`'s append/advance/toggle bookkeeping,
  persistence across manager instances (simulating session resume),
  `MirrorDirtyError` + `"stash"`/`"discard"` resolution.
- `test/test_checkpoints.py` — pre-existing `RootMirrorManager` coverage
  (lazy creation, no-op, multi-root isolation), updated for the new
  `CheckpointState`-returning `undo`/`rollback` signatures.
- `test/test_server_integration.py` (`checkpoint.*` section) — full wire
  protocol against a real `aiohttp` server + real WebSocket: `checkpoint.list`,
  `checkpoint.undo` (with the `EVT_CHECKPOINT_STATE` push), rollback +
  roll-forward, and the dirty-tree `needs_confirmation` → `resolution=stash`
  retry flow. The checkpoint history is seeded directly via
  `RootMirrorManager` (the same on-disk artifacts a real tool call would
  produce) so the test exercises the server/protocol layer without needing
  an LLM.
