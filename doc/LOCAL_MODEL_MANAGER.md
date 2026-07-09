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
via a hand-rolled `urllib.request`-based downloader (`kodo/llms/local/_http.py`)
that keeps its own `<file>.part` file across interruptions and resumes it with
a standard `Range: bytes=<size>-` request. This is the same "manual
`urllib` + progress callback" pattern `kodo.llms.llamacpp._installer` already
uses for the llama.cpp binary itself — no new dependency.

---

## 2. Package layout

```
kodo/llms/local/
  __init__.py    public surface
  _types.py      FileRole, FileStatus, ModelFile, ModelRecord, DownloadProgress,
                 ProgressCallback, and every exception this package raises
  _hf.py         HF metadata only: resolve_file, list_repo_files, detect_shard_group
  _http.py       download_to_part_file — the resumable/pausable byte transfer
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
    mmproj.gguf              # finished MMPROJ file, if attached
```

Each model gets its **own subdirectory** (`root_dir/<sanitized model_id>/`)
so two models can never collide on filename, and `uninstall` is a single
`shutil.rmtree`.

`manager-state.json` is the single source of truth for *what* files belong to
a model and their last-known status — a plain JSON object keyed by
`model_id`, each value a serialized `ModelRecord` (see `_state.py`). It is
**not** the source of truth for *where* a resumed download continues from —
that's always the real size of the `.part` file on disk
(`download_to_part_file` reads `part_path.stat().st_size` directly). This
split matters: `ModelFile.downloaded_bytes` in the state file is only updated
at status transitions (download start/pause/fail/complete), **not on every
chunk** — writing the full state JSON on every 64 KiB chunk of a 40 GB model
would be its own bottleneck. A `list_models()`/`get_record()` call made while
a download is actively running may show a slightly stale byte count; use
`progress_cb` (passed to `download_model`/`resume_download`/`download_mmproj`)
for live progress instead.

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
  to stop. Implemented as a `threading.Event` held in-memory, per
  `LocalModelManager` instance, keyed by `model_id`. A no-op if nothing is
  currently downloading for that id. The transfer loop checks the event
  between chunks (`_http._CHUNK_SIZE`, 64 KiB) and raises
  `DownloadPausedError` internally, which `_manager.py` catches and turns
  into `FileStatus.PAUSED` — never an exception the caller sees. The `.part`
  file is left exactly as it was.
- **Resume** (`resume_download(model_id)`) — re-downloads every file that
  isn't `COMPLETED`, regardless of *why* it isn't: user-paused, a network
  failure (`FAILED`), or the process died mid-transfer (stuck
  `DOWNLOADING` — indistinguishable from a resumable state once read back
  from disk after a restart, since the real resume point is the `.part`
  file's byte size, not the state file's status string). One method covers
  all three causes.

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
false`, download not started yet) and *then* kicks off the transfer on a
worker thread via `_run_background_download`. There is no byte-level
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

---

## 10. Testing

`test/test_llms_local.py` runs against a tiny in-process HTTP server
(`http.server.ThreadingHTTPServer` + a handler with real `Range`/`206`
support) instead of mocking `_http.py`'s transfer logic away — `resolve_file`
and `list_repo_files` are monkeypatched to point at it, but the real
chunked-download-with-resume code path runs end-to-end, including a
byte-for-byte comparison after a pause-then-resume cycle and after a
"server ignores the Range header" restart. Pausing mid-download is
deterministic (no timing/sleep races): the test's `progress_cb` calls
`pause_download` directly once a byte threshold is crossed, which runs
synchronously on the same thread as the transfer loop, so the very next
chunk-boundary check sees it.
