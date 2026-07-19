# Local Model Manager — Stateful HuggingFace GGUF Downloads

> `kodo.llms.local.LocalModelManager`: download, pause/resume, multi-file
> (split GGUF) deduction, mmproj companions, and uninstall for GGUF models
> fetched from HuggingFace Hub. Replaces the old
> `kodo.llms.llamacpp._downloader` implementation, which only wrapped
> `huggingface_hub.hf_hub_download` for a single file with no pause/resume,
> no multi-file support, and no mmproj.

Companion to [LLM_REGISTRY.md](LLM_REGISTRY.md) (the local-model *registry* —
what's known/addable — as opposed to this doc, which covers the *manager* —
what's actually been downloaded) and [LOCAL_INFERENCE.md](LOCAL_INFERENCE.md)
(what happens once a model is running). Code lives entirely under
`kodo/llms/local/`.

---

## 1. Why not just call `hf_hub_download` again?

The old `_downloader.py` called `huggingface_hub.hf_hub_download` and relied
on its own resume-on-retry behavior. That library, as pinned by this project,
no longer has that behavior: `file_download.py`'s `_download_to_tmp_and_move`
writes every download to a **process-unique** `<etag>.<random>.incomplete`
temp file and **unconditionally deletes it on any failure or interruption**
(a deliberate 2025 change — huggingface_hub PR #4228 — to avoid corruption
from broken `flock` semantics on some network filesystems like NFS/Lustre).
Calling `hf_hub_download` again after a interruption therefore restarts from
byte zero — there is no library-level resume left to build "pause/resume" on.

`kodo.llms.local` addresses this by **bypassing `hf_hub_download` for the
byte transfer entirely**. It still uses `huggingface_hub` for *metadata* —
resolving a repo file to a URL/ETag/size, listing repo files, and following
HF's own "don't send the auth header to a signed CDN redirect" rule (mirrored
from `file_download.py`'s own metadata-resolution logic — see
`kodo/llms/local/_hf.py`) — but does the actual streaming download itself,
via a hand-rolled `aiohttp`-based downloader (`kodo/llms/local/_http.py`,
`download_to_part_file`) that keeps its own `<file>.part` file across
interruptions.

**A single file downloads as several concurrent partial-GET streams, not
one.** Once the file's size is known (the normal case — HF metadata almost
always reports it), it's split into fixed-size 4 MiB ranges (plus one shorter
final range for the remainder), and up to `_DEFAULT_PARALLELISM` (8) worker
coroutines pull ranges off a shared in-memory work list and issue their own
`Range: bytes=<start>-<end>` GET, writing straight into the right offset of a
single pre-sized random-access `.part` file handle. This is a best-effort
work-sharing pool: whichever worker finishes first just grabs the next
not-yet-fetched range, so one slow chunk never blocks the others. Everything
runs on one event-loop thread — popping the next range and writing a
finished chunk's bytes never overlaps across workers (each chunk owns a
disjoint byte range), so none of this needs a lock. See §4 for how resuming
a download that was interrupted mid-chunk works, and §6 for what pausing
actually stops. A file whose size *isn't* known upfront (rare) falls back to
a single-stream sequential GET — chunking a range you can't compute the end
of isn't possible.

Every method that transfers bytes (`download_model`, `resume_download`,
`download_mmproj`) is a genuine coroutine — no worker thread anywhere in this
package (see §9).

---

## 2. Package layout

```
kodo/llms/local/
  __init__.py    public surface
  _types.py      FileRole, FileStatus, ModelFile, ModelRecord, DownloadProgress,
                 ProgressCallback, and every exception this package raises
  _hf.py         HF metadata only: resolve_file, list_repo_files, detect_shard_group
  _http.py       download_to_part_file — the resumable/pausable, parallel-chunked byte transfer
  _state.py      JSON (de)serialization of ModelRecord <-> manager-state.json
  _manager.py    LocalModelManager — the class that ties it all together
```

**Zero dependency on any other `kodo` package** — by design, so that
`kodo.llms.llamacpp` (and anything else) can depend on this without a cycle.
`LocalModelManager` has no notion of `~/.kodo`, `LocalLLMEntry`, settings.json,
or llama-server; every method takes plain arguments (`model_id: str`,
`repo_id: str`, `filename: str`, `root_dir: Path`, ...). Translating a
registry entry or a settings-derived directory into those calls is entirely
the caller's job — see §6.

**Independent from the model *registry*, deliberately.** `kodo.llms._local_registry`
answers "what models are known/addable" (hardcoded + user-added
`LocalLLMEntry` rows). `LocalModelManager` answers "what have I actually
downloaded" — it only knows about a `model_id` once something has called
`download_model` for it at least once. The registry is one *source of input*
for the manager (a caller reads a `LocalLLMEntry`'s `repo_id`/`filename` and
passes them to `download_model`), but the two have no other coupling: the
manager doesn't validate against the registry, and the registry doesn't know
the manager exists.

---

## 3. Data model

```python
class FileRole(enum.StrEnum):
    MAIN = "main"        # the only file, or shard 1 of a split GGUF
    SHARD = "shard"       # a non-first shard — llama-server finds these itself
    MMPROJ = "mmproj"     # a multimodal-projector companion file

class FileStatus(enum.StrEnum):
    PENDING | DOWNLOADING | PAUSED | COMPLETED | FAILED

@dataclass
class ModelFile:
    filename: str; role: FileRole; repo_id: str; revision: str
    size: int | None; etag: str | None
    downloaded_bytes: int   # informational only, see §5
    status: FileStatus; error: str
    bytes_per_second: float | None  # trailing ~10s rate while DOWNLOADING, see §11a

@dataclass
class ModelRecord:
    model_id: str; repo_id: str; revision: str; commit_hash: str | None
    files: list[ModelFile]
    created_at: str; updated_at: str
    # properties: main_files, primary_file, mmproj_file, is_installed, has_resumable_work
```

Every `ModelFile` carries its **own** `repo_id`/`revision` — not just the
`ModelRecord`'s — because an mmproj file is frequently fetched from a
*different* repo than its parent model. `resolve_file` is always called with
the file's own `repo_id`, never the record's.

`ModelRecord.is_installed` only requires the MAIN/SHARD files to be complete
— an mmproj-less model is still usable for text inference, so mmproj status
never gates "installed".

---

## 4. On-disk layout and state file

```
<root_dir>/
  manager-state.json
  <sanitized model_id>/
    model.gguf              # finished MAIN file
    model.gguf.part         # in-progress/paused (never present alongside the finished file)
    model.gguf.part.chunks  # sidecar: which 4 MiB ranges of the .part file are done
    mmproj.gguf              # finished MMPROJ file, if attached
```

Each model gets its **own subdirectory** (`root_dir/<sanitized model_id>/`)
so two models can never collide on filename, and `uninstall` is a single
`shutil.rmtree`.

`manager-state.json` is the single source of truth for *what* files belong to
a model and their last-known status — a plain JSON object keyed by
`model_id`, each value a serialized `ModelRecord` (see `_state.py`). It is
**not** the source of truth for *where* a resumed download continues from.
For the single-stream fallback (file size unknown) that's still the real
size of the `.part` file on disk, same as before. For the normal parallel
path it can't be: `_http._download_parallel` pre-truncates `.part` to the
*full* expected size before any chunk is written (so every worker can seek
anywhere in it immediately), which means the file's size on disk is `total_size`
from the very first instant of a transfer — no longer any indication of how
much of it is real data. The `<file>.part.chunks` sidecar is the actual source
of truth: a small JSON object (`{"chunk_size", "total_size", "completed": [...]}`)
recording which chunk *indices* have actually landed, flushed at most once a
second (mirroring `_FLUSH_INTERVAL_SECONDS` below) as chunks complete, and
deleted once the transfer finishes. A `.part` file with **no** sidecar is
either a fully-finished-and-cleaned-up transfer or a pre-upgrade file from the
old single-stream downloader; both are handled by treating its on-disk size as
a trustworthy contiguous prefix (see `_http._download_parallel`'s docstring).

Either way, `ModelFile.downloaded_bytes` in `manager-state.json` is only
updated at status transitions (download start/pause/fail/complete) plus a
throttled live update while running (**not on every chunk** — writing the
full state JSON on every 4 MiB chunk of a 40 GB model would be its own
bottleneck). A `list_models()`/`get_record()` call made while a download is
actively running may show a slightly stale byte count; use `progress_cb`
(passed to `download_model`/`resume_download`/`download_mmproj`) for live
progress instead.

---

## 5. Multi-file (split GGUF) deduction

`detect_shard_group` (`_hf.py`) recognizes llama.cpp's split-GGUF naming
convention — `<prefix>-NNNNN-of-MMMMM.gguf` — and works from **any** shard
index, not just the first: given `model-00002-of-00005.gguf`, it deduces all
five filenames, then calls `list_repo_files` to confirm every one actually
exists in the repo before committing to the download. A missing sibling
raises `ShardResolutionError` immediately, before any bytes are fetched. A
filename that doesn't match the pattern is treated as a self-contained
single-file model (returns `[filename]` unchanged) — no behavior change for
the common case.

`ModelRecord.primary_file` is always shard index 1 (`FileRole.MAIN`) — that's
the only path a caller (or `llama-server`) ever needs, since llama.cpp
auto-discovers the remaining shards from files alongside the one it's given.

---

## 6. Pause / resume semantics

Two distinct, deliberately separate concepts:

- **Pause** (`pause_download(model_id)`) — signals an **in-flight** transfer
  to stop. Implemented as an `asyncio.Event` held in-memory, per
  `LocalModelManager` instance, keyed by `model_id`. A no-op if nothing is
  currently downloading for that id. Every chunk worker checks the event
  before starting its *next* range request — since up to `_DEFAULT_PARALLELISM`
  (8) of them run concurrently, up to that many chunks' worth of data (up to
  ~32 MiB, not one 64 KiB chunk as in the old single-stream design) can still
  land after `pause_download` is called, as whichever workers are already
  mid-request finish that one request before checking the event and
  stopping. `DownloadPausedError` propagates internally, which `_manager.py`
  catches and turns into `FileStatus.PAUSED` — never an exception the caller
  sees. The `.part` file and its `.chunks` sidecar (§4) are left exactly as
  they were, chunk-complete state intact.
- **Resume** (`resume_download(model_id)`) — re-downloads every file that
  isn't `COMPLETED`, regardless of *why* it isn't: user-paused, a network
  failure (`FAILED`), or the process died mid-transfer (stuck
  `DOWNLOADING` — indistinguishable from a resumable state once read back
  from disk after a restart). The real resume point is the `.chunks` sidecar
  (which chunk indices are done), not the state file's status string nor the
  `.part` file's byte size (see §4 for why the latter no longer works once a
  transfer pre-sizes the file upfront). One method covers all three causes,
  and only refetches whatever chunks the sidecar doesn't already have —
  including a chunk that failed mid-network-error, so a `FAILED` transfer's
  resume isn't a full restart either.

**A server that stops honoring `Range` mid-transfer now fails loudly instead
of silently restarting.** The old single-stream downloader could recover from
a server that ignored a `Range` header (treating a `200` response as "sending
the whole file from byte 0" and restarting the `.part` file) because there
was only ever one request in flight. A parallel chunked transfer can't do
that safely: a `200` response to one worker's *bounded* range request means
the full file arrived at the wrong offset for that chunk, other chunk workers
may already be mid-flight, and blindly absorbing that response as "the whole
file, actually" risks each of up to 8 concurrent workers doing it
independently, each retransmitting the entire file. `_http._download_parallel`
instead raises `DownloadError` the moment any worker gets a bare `200` for a
request that wasn't for the entire file in one chunk (i.e. more than one
chunk exists) — see `test_resume_fails_clearly_when_server_stops_honoring_range`
in `test/test_llms_local.py`. In practice this is a non-issue against HF's
actual CDN, which reliably honors `Range` (the existing single-stream resume
feature already depended on that); it only matters for a misbehaving proxy or
mirror.

Because the pause signal is in-memory, it only correlates with a download
started by *the same* `LocalModelManager` instance — a fresh instance after a
process restart can't pause a download it never started (there's nothing
running to pause), but it can always `resume_download` anything left
incomplete, since that only depends on-disk state. This is why the class
docstring calls out reusing one instance for the process lifetime: see §6 of
this doc's sibling section on integration, and the class's own docstring.

`list_resumable()` returns every `model_id` with incomplete files and no
currently-active transfer — the list a caller would offer the user as
"resume interrupted downloads?" after a restart.

---

## 7. mmproj companions

`download_mmproj(model_id, repo_id, filename, ...)` requires `model_id` to
already have a record (i.e. `download_model` was called for it at least
once — it doesn't need to have *finished*). An mmproj file is always
attached to a specific model; there's no standalone mmproj download.
Attaching a new mmproj replaces any previous one on that record. mmproj
status is entirely independent of `is_installed` (§3) and of the MAIN/SHARD
files' pause/resume state — pausing/resuming the model doesn't touch its
mmproj file and vice versa (`resume_download`, however, *does* pick up an
incomplete mmproj file alongside any incomplete MAIN/SHARD files, since it
just resumes "everything incomplete" on the record).

---

## 8. HuggingFace access tokens

`token: str | None` is a **per-call** parameter on every method that talks to
HF (`download_model`, `resume_download`, `download_mmproj`) — this package
never persists a token anywhere. This was a deliberate scope decision: the
long-term plan is for kodo-vsix to own token storage in VS Code
`SecretStorage`, resolved via a pull-protocol WS message mirroring the
existing cloud-API-key flow (`api_key.request`/`api_key.revoke`, see
[LLM_REGISTRY.md](LLM_REGISTRY.md) §6) — but that WS/UI wiring is explicit
follow-up work (§9), not part of this package. Passing a plain `token`
argument through keeps `LocalModelManager` decoupled from however a caller
ends up resolving one.

---

## 9. Integration with `kodo.llms.llamacpp` — and what's *not* wired yet

There is no adapter module any more — `kodo.llms.llamacpp._downloader` (the
thin glue that used to keep the old `download_model`/`delete_model`/
`get_model_path` function signatures alive) has been deleted. Every caller
now depends on `LocalModelManager` directly, reached via
`kodo.llms.llamacpp.get_local_model_manager(kodo_dir)`
(`kodo/llms/llamacpp/_manager.py`), which:

1. Resolves the models directory (`llm_models_dir` in `settings.json`,
   falling back to `~/.kodo/llama.cpp/models`).
2. Looks up (or lazily creates) a **process-wide** `LocalModelManager` for
   that directory, cached in a module-level `dict[Path, LocalModelManager]`.
   `LocalModelManager` itself is not a singleton — nothing in its own
   definition prevents constructing more than one — this cache exists purely
   so the `local_llm.pause` WS handler reaches the *same* in-memory
   cancel-event as the `download_model`/`resume_download` call it's meant to
   interrupt, which requires reusing one instance across separate WS request
   handlers within one server process (kodo-server is already a single
   long-lived process per user). It also means reconciliation (§11) only
   ever runs once per models directory per process.

The two call sites:

- `ensure_llama_running` (`kodo/llms/llamacpp/_manager.py`, same module) calls
  `get_local_model_manager(kodo_dir).get_model_path(entry.name)` directly for
  `hardcoded_hf`/`custom_hf` entries.
- `server/_app.py`'s `_start_background` (an `aiohttp` `on_startup` hook, so
  this runs unconditionally at every server boot, before any client
  connects) calls `get_local_model_manager(user_dir)` while checking whether
  an already-running llama-server process can be adopted — as a side effect,
  this is what makes restart reconciliation (§11) run right at startup
  rather than waiting for the first `local_llm.*` WS request.
- `server/_app.py`'s `local_llm.*` WS handlers
  (`_handle_local_llm_install`/`_handle_local_llm_resume`/
  `_handle_local_llm_pause`/`_handle_local_llm_uninstall`/
  `_handle_local_llm_remove`, plus `_local_entry_installed`/
  `_local_entry_installed_path`) call `get_local_model_manager(kodo_dir)` and
  then `.download_model(...)`/`.resume_download(...)` (both fired via a
  `_run_background_download` helper that logs a `LocalModelError` and, either
  way, pushes one more `local_llm.registry_state` once the transfer settles —
  see §11), `.pause_download(...)`, `.uninstall(...)`, `.get_model_path(...)`
  on the returned instance.

`download_model`/`resume_download`/`download_mmproj` are `async def` —
`_run_background_download` just `await`s the coroutine as an
`asyncio.create_task`, no worker thread involved anywhere in the byte
transfer (see §1). The two HF metadata calls each still makes
(`resolve_file`, `list_repo_files` — real synchronous `huggingface_hub`
network calls) are individually wrapped in `asyncio.to_thread` inside
`__run_transfer`, so a slow HF Hub round trip doesn't stall the whole
server's event loop (every other WS connection's requests) for its
duration — only the byte transfer itself is native async.

**pause/resume are now wired** (§11) — `local_llm.pause`/`local_llm.resume` WS
messages reach `pause_download`/`resume_download` directly. **Still not wired
up** (tracked as follow-up): no HF-token entry UI, no mmproj UI. `local-llm-
index.json` — the old flat `{name: path}` index file the pre-
`LocalModelManager` downloader used to maintain — is retired in favor of
`manager-state.json`; nothing else read it. The manager's HF-token and mmproj
capabilities are fully implemented and tested (see `test/test_llms_local.py`)
but not yet reachable from kodo-vsix.

---

## 11. kodo-vsix integration: disk-polled progress, not a WS push

Every `local_llm.install`/`local_llm.resume`/`local_llm.pause` WS handler
(`server/_app.py`) is fire-and-forget: it replies immediately with
`local_llm.registry_state` (the "kickoff" push — model still `installed:
false`, download not started yet) and *then* kicks off the transfer as a
background `asyncio` task via `_run_background_download` — no worker thread,
since the transfer itself is native async (§1, §9). There is no byte-level
progress event on the wire at all any more (the old `local_llm.install.
progress` event is gone) — but `_run_background_download`'s `run()` coroutine
does push **one more** `local_llm.registry_state` on the same connection once
`work` (the `download_model`/`resume_download` call) finishes, success or
failure (`try`/`finally`, so a `LocalModelError` doesn't skip it). This is
what lets the requesting window's sidebar and Local Inference Settings panel
pick up `installed`/`installed_path` flipping to the completed state without
polling or reopening the panel — every other `local_llm.*` mutation already
replies with fresh state on completion, this just extends that pattern to a
completion that happens asynchronously after the reply. The kickoff reply is
sent (not merely scheduled) *before* `_run_background_download` creates its
task specifically so the two `registry_state` events can't race each other
onto the wire out of order — see `test_local_llm_install_pushes_registry_
state_again_on_completion` in `test/test_server_integration.py`. Still
per-connection only, not a broadcast (see below) — a *different* window than
the one that clicked install won't see the flip until it reconnects or
reopens the panel. Worse, even the *originating* window can miss it: the
completion push targets whatever `Connection` object was live when the
request was handled, and `Connection.send` silently no-ops if that socket has
since closed — which is a real risk for a multi-minute HuggingFace transfer,
since any WS reconnect in between (sleep, idle timeout, network blip) leaves
the push aimed at a dead connection forever, with nothing to fall back on. See
below for how kodo-vsix's disk poller (§11 continued) now closes both gaps
client-side, without any new broadcast infrastructure on this end. Instead:

- If `work` raised a `LocalModelError`, `_run_background_download` also sends
  an `error` event (`code: "local_llm_error"`, the same code every
  synchronous `local_llm.*` validation failure already uses) *before* the
  completion `registry_state` push. kodo-vsix's top-level WS event handler
  (`extension.ts`) shows any `local_llm_error` via `vscode.window.
  showErrorMessage`, which VS Code parks in the bottom-right corner until the
  user dismisses it — this is what surfaces a background download failure
  (e.g. a typo'd `repo_id` in the registry, or a HuggingFace 401) to the
  user at all. Before this exception path existed, such a failure only ever
  reached the server log; clicking "Download and Install" looked like
  nothing had happened.
- A failure inside `download_model` before any shard is even known (a bad
  `repo_id`/`revision`, or a gated repo without a valid token — both raise
  out of `list_repo_files`, before the shard-deduction `mutate()` call ever
  runs) used to leave `manager-state.json` completely untouched, so
  kodo-vsix's disk poll had nothing to reflect either. `download_model` now
  seeds a placeholder `ModelFile` for the requested `model_id`/`filename`
  *before* the HuggingFace round trip, and marks it `FileStatus.FAILED` with
  the exception message if that round trip raises — the same
  `FileStatus.FAILED`/`error` fields a mid-transfer failure already writes
  (see `__run_transfer` below), so the panel's existing "Failed: `<message>`"
  download row and Cancel button (→ `local_llm.uninstall`) handle this case
  for free, no new UI needed.
- The Local Inference Settings panel's install button disables itself and
  relabels to "Starting download…" synchronously in its own click handler
  (`local-inference-settings-panel.ts`), before `postMessage` — bridging the
  gap between the click and the first state update (kickoff registry_state,
  error event, or a disk-poll tick) actually reaching the webview. No new
  tracked state: the next `'update'` message always rebuilds the button from
  scratch, so a silent early failure just gets a normal clickable button
  back rather than one stuck reading "Starting download…" forever.

- `__run_transfer` (`_manager.py`) unconditionally persists the active file's
  `downloaded_bytes`/`size`/`status` to `manager-state.json` at most once a
  second (`_FLUSH_INTERVAL_SECONDS`), regardless of whether a `progress_cb`
  was passed — this is what makes the state file a live-enough source for a
  UI to poll, not just a checkpoint at start/pause/fail/complete.
- **Every target's size is resolved before shard 1's first byte, not just its
  own shard's.** `overall_total()`/`overall_bytes_total` (used by both
  `DownloadProgress` and, via `manager-state.json`, kodo-vsix's
  `local-model-downloads.ts summarize()`) is `None` unless *every* target
  file's `size` is known. Previously, a split GGUF's later shards only had
  `resolve_file` called on them once the loop reached their own turn, so for
  the entire duration of shard 1 (and shard 2, etc.) `overall_bytes_total`
  stayed `None` — kodo-vsix's progress bar reads that as "unknown total" and
  renders a permanently empty (0%) bar, even though `bytes_downloaded` (and
  the byte-count label) was climbing correctly. `__run_transfer` now runs a
  size-only pre-pass over every target with `size is None` *before* the
  per-file download loop, persisting each resolved size immediately. Failures
  in the pre-pass are swallowed — the real `resolve_file` call (and its error
  handling) still happens per-file when that file's turn actually comes up —
  so this only ever improves how early a total becomes known, never turns a
  transient metadata hiccup into a hard failure. This also self-heals a
  paused/interrupted download from before this fix: `resume_download` routes
  through the same `__run_transfer`, so any still-`None` shard size gets
  resolved on the next resume.
- `save_state` (`_state.py`) flushes and `fsync`s before the atomic
  tmp-file replace, so a reader racing the write sees either the fully old or
  fully new content, never a torn write.
- kodo-vsix (`local-model-downloads.ts`) resolves the same models directory
  the server would (`llm_models_dir` in `settings.json`, else
  `~/.kodo/llama.cpp/models`) and polls `manager-state.json` once a second
  (gated by an `fs.statSync` mtime check so an idle machine costs one stat()
  call and nothing else), tolerating any read/parse error silently and
  retrying next tick — the same eventual-consistency stance every open
  VS Code window takes independently. This is a deliberate architecture
  choice over WS broadcast: `ConnectionRegistry` doesn't track connections as
  a group today, so there is no way to push an event to every open window's
  control connection, only to the one that issued a given request. Reading a
  shared file instead means every window converges on the same state without
  any new broadcast infrastructure, and a download survives its originating
  window closing for free (the transfer thread was never tied to that
  connection to begin with).
- **Completion detection, every window** (`extension.ts`): the same poller
  that reports byte-level progress also closes the "does `installed` ever
  flip" gap above, entirely client-side. `summarize()` (`local-model-
  downloads.ts`) already stops reporting a model once every file is
  `completed` — it just drops out of the returned map, treated as "not a
  download in progress" any more. `activate()`'s poll callback now diffs the
  previous tick's tracked names against the new ones; the moment a name it
  was tracking disappears (finished installing, or removed/uninstalled), it
  re-sends the `hello` control request. Since `hello` (role `control`) is a
  cheap, side-effect-free snapshot request already handled server-side
  (`_make_hello_handler`, §"hello" above) and its `hello.ack` handler already
  refreshes `localRegistryState` — and with it both the sidebar and the
  Local Inference Settings panel — this needed zero protocol changes. Because
  every open window runs this poller independently against the same
  `manager-state.json`, every window notices the completion on its own next
  tick and re-syncs itself, regardless of which window's connection actually
  issued the `local_llm.install`/`resume` request and regardless of whether
  that connection survived long enough to receive the server's completion
  push above.
- **Restart reconciliation**: `LocalModelManager.__init__` forces any file
  still marked `DOWNLOADING` in the loaded state to `PAUSED` before anything
  else touches it — safe because a fresh instance's own `__cancel_events` is
  necessarily empty, so no transfer *this* instance started could be
  responsible for that status. This is what turns "kodo-server was killed or
  the host restarted mid-download" into a `PAUSED` download kodo-vsix offers
  to resume, rather than a progress bar stuck showing "downloading" forever.
  A no-op (doesn't even create `manager-state.json`) when nothing is stale.

### 11a. Download speed: a 10s sliding window, computed server-side

`ModelFile.bytes_per_second` (`_types.py`) carries a trailing-~10-second
transfer-rate estimate, persisted to `manager-state.json` right alongside
`downloaded_bytes` — kodo-vsix reads it off the same disk-polled snapshot it
already uses for byte counts (§11 above), no new protocol surface. Computed
server-side (not client-side in `local-model-downloads.ts`) so every open
VS Code window agrees on one rate regardless of when it started polling, and
so the value survives a window closing and reopening mid-download.

- `__run_transfer`'s per-file `on_bytes` closure (`_manager.py`) keeps a
  `deque[tuple[float, int]]` of `(time.monotonic(), cumulative_bytes)`
  samples, one per chunk actually written to disk (i.e. on every call, not
  throttled to `_FLUSH_INTERVAL_SECONDS` — throttling the *sampling* as well
  as the *flush* would starve the window down to ~1 sample/sec and defeat
  measuring a genuine 10s span). `_windowed_bytes_per_second` (module-level,
  `_manager.py`) prunes samples older than `_SPEED_WINDOW_SECONDS` (10.0) off
  the left, always keeping at least the just-appended sample, and returns
  `(bytes_now - bytes_oldest_in_window) / (time_now - time_oldest_in_window)`
  — `None` until a second sample exists.
- The computed rate is only ever *persisted* at the existing
  `_FLUSH_INTERVAL_SECONDS` cadence (piggybacking on the same
  `__set_file_status` call `downloaded_bytes` already rides), so this adds no
  new disk-write pressure.
- `bytes_per_second` is explicitly reset to `None` on every transition out of
  live `DOWNLOADING` — the start of a (re)transfer attempt, `PAUSED`,
  `FAILED`, and `COMPLETED` — so a paused or finished download never shows a
  frozen rate left over from the moment it stopped moving.
- kodo-vsix (`local-model-downloads.ts` `summarize()`) sums each file's
  `bytes_per_second` across a model's files (only one file downloads at a
  time per `__run_transfer`'s per-file loop, so in practice at most one
  non-`null` value contributes at once) into `LocalDownloadState.
  bytes_per_second`, and the Local Inference Settings panel
  (`local-inference-settings-panel.ts`) formats it with a small
  auto-scaling `formatSpeed()` helper (B/s → KB/s → MB/s → GB/s, 1024-based)
  next to the existing byte-count label, shown only while `status ===
  'downloading'`.

---

## 10. Testing

`test/test_llms_local.py` runs against a tiny in-process HTTP server
(`http.server.ThreadingHTTPServer` + a handler with real bounded (`bytes=
start-end`) and open-ended (`bytes=start-`) `Range`/`206` support) instead of
mocking `_http.py`'s transfer logic away — `resolve_file` and
`list_repo_files` are monkeypatched to point at it, but the real
chunked-download-with-resume code path runs end-to-end, including a
byte-for-byte comparison after a pause-then-resume cycle, and
`test_download_uses_multiple_concurrent_connections` proves the transfer
really does run several range requests at once (the fake server tracks a
concurrent-request high-water mark, with a small artificial per-request
delay so overlap is observable regardless of how fast loopback I/O is).

Every test that exercises a specific pause **byte boundary**
(`test_pause_then_resume_produces_identical_bytes`, the Range-ignored-on-
resume test below) monkeypatches `_http._DEFAULT_PARALLELISM` down to `1` —
with several concurrent chunk workers over near-instant loopback I/O, "pause
once >=N bytes are downloaded" would otherwise race the remaining chunks to
completion before the pause takes effect. This has to be a monkeypatch of
the module constant read *inside* `download_to_part_file`'s body at call
time, not a keyword override of a function-signature default — a default
argument value is bound once at function-definition time, so patching the
constant wouldn't reach a signature that captured the old value at import.

A server that stops honoring `Range` mid-transfer no longer "restarts
cleanly" the way the old single-stream downloader could — see §6 for why —
so `test_resume_fails_clearly_when_server_stops_honoring_range` (replacing
the old `test_resume_when_server_ignores_range_restarts_cleanly`) asserts the
new behavior: a clear `DownloadError` instead of silent corruption.

Pausing mid-download is deterministic (no timing/sleep races): the test's
`progress_cb` calls `pause_download` directly once a byte threshold is
crossed, which runs synchronously on the same event-loop thread as the
transfer, so the very next chunk-boundary check in that worker sees it (with
parallelism forced to 1, there's only ever the one worker to check).

---

## 12. Update checking (ETag comparison) and "Update" (uninstall + reinstall)

Whether an installed model's remote GGUF has since changed — a re-quant, a
bug fix, a re-upload — is answered without downloading anything, by
comparing the ETag recorded at install time against a freshly-resolved one.

- **`LocalModelManager.check_for_update(model_id, *, token=None) -> bool`**
  (`_manager.py`) re-resolves every MAIN/SHARD file's current HF metadata via
  the same `resolve_file` call `download_model` makes before transferring
  bytes — a metadata-only HEAD-equivalent request, no bytes fetched — and
  compares the returned ETag against `ModelFile.etag` as recorded on disk.
  `True` only once every file resolves successfully and at least one ETag
  differs. mmproj files are excluded, mirroring `ModelRecord.is_installed`'s
  own MAIN/SHARD-only definition of "this model". Deliberately permissive on
  failure: not installed, no recorded ETag yet (a file completed before this
  package tracked ETags), or the HF round trip itself failing (renamed/
  deleted repo, transient network error) all return `False` rather than
  raising — from a caller's point of view "confirmed current" and "couldn't
  tell" are both just "nothing to flag right now" (see the method's
  docstring, and `test_check_for_update_*` in `test/test_llms_local.py`).
  Like every other read on this class, it costs nothing extra to call
  speculatively.

- **`local_llm.check_updates {names: [str, ...]}`** (server `_app.py`
  `_handle_local_llm_check_updates`) is the WS entry point kodo-vsix's Local
  Inference Settings panel calls every time it opens, passing every
  currently-installed `hardcoded_hf`/`custom_hf` model name (client-filtered
  via `isDownloadableLocalEntry(kind) && entry.installed` in
  `extension.ts`'s `_sendCheckLocalLlmUpdates`) — a `custom_file`/
  `custom_server_url` entry isn't HF-backed and has no remote to compare
  against, so kodo-vsix never includes one, and the server independently
  skips any name that isn't a known `hardcoded_hf`/`custom_hf` entry (a stale
  client-side list is not an error). **Fire-and-forget, deliberately**: the
  client sends it and moves on (`_sendControl`, not `sendControlAwait`) —
  checking N models each costs a real HF metadata round trip, and the
  webview shouldn't block opening on that. The handler runs the scan as a
  background `asyncio.create_task` and, once every name has been checked,
  pushes one `local_llm.updates_available {updatable: [name, ...]}` event on
  the same connection — an empty list is sent (not omitted) so a stale
  "updates available" banner from a previous scan clears on a clean rescan.
  kodo-vsix's top-level WS dispatcher (`extension.ts`
  `_onLocalLlmUpdatesAvailable`) *replaces* (never merges)
  `localUpdatableNamesState` with each reply, then re-pushes panel state —
  the Local Inference Settings panel's `#updates-banner` (yellow, only
  visible when the list is non-empty) and each installed card's "Update"
  button (`local-inference-settings-panel.ts` `renderUpdatesBanner`/
  `renderModelCard`) both read straight off it.

- **`local_llm.update {name}`** (`_handle_local_llm_update`) is deliberately
  *not* a new atomic "re-download in place" code path. It's composed from
  the exact same two calls `local_llm.uninstall`/`local_llm.install` already
  make — `LocalModelManager.uninstall(name)` (synchronous, same as
  `local_llm.uninstall`) immediately followed by the same fire-and-forget
  `download_model` dispatch via `_run_background_download` that
  `local_llm.install` uses — so an update goes through the exact same
  manager-state transitions (installed -> uninstalled -> kickoff ->
  downloading -> installed/failed) a user manually clicking Uninstall then
  Install would produce, rather than a bespoke shortcut that could drift out
  of sync with those two paths. Two `local_llm.registry_state` pushes follow,
  same shape as install's: one right after the uninstall (already correctly
  showing `installed: false` — no separate "kickoff" state needed, since
  post-uninstall *is* pre-download), then one more once the fresh download
  settles. Only defined for `hardcoded_hf`/`custom_hf` entries — same gate
  `local_llm.install` uses — and replies with the standard
  `local_llm_error`/`error` event for an unknown or non-downloadable name.
  See `test_local_llm_update_uninstalls_then_reinstalls` in
  `test/test_server_integration.py`.

- **kodo-vsix UI**: the "Update" button only ever renders on an installed,
  HF-backed card whose name is currently in `updatableNames` — clicking it
  disables the button and relabels it "Updating…" (the same
  immediate-feedback pattern `local-inference-settings-panel.ts` already
  uses for "Download and Install"/Pause/Resume), then fires `local_llm.
  update`. No separate re-enable path exists: the entry briefly reports
  `installed: false` mid-update, at which point the card naturally falls
  back to rendering "Download and Install" (or "Downloading — see progress
  above" once the reinstall's transfer starts) instead of "Update" — the
  normal install-flow rendering already covers every stage, so nothing
  update-specific needs to persist across the transition. `extension.ts`
  also drops the name from `localUpdatableNamesState` the moment the button
  is clicked, immediately (not just optimistically — the triggered update is
  what actually brings the file back in sync), rather than waiting for a
  fresh `check_updates` scan.

- **HF tokens**: like `download_model`, `check_for_update` accepts a
  per-call `token` but nothing in this feature passes one yet — gated repos
  are out of scope here the same way they're out of scope for install (§8).
