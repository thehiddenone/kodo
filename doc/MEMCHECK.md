# kodo-memcheck — measuring a quant's memory empirically

`kodo-memcheck` loads one local-registry quant in llama-server, reads the
buffers llama-server reports allocating, stops it, and prints the total to
stdout. It's the empirical check for a catalog entry's `min_memory`/`memory`
([ADDING_A_LOCAL_LLM.md](ADDING_A_LOCAL_LLM.md)), so you don't have to rely on
an estimate derived from the GGUF header.

```
kodo-memcheck ENTRY [-c|--ctx-size N] [--rope-scale F] [--yarn-orig-ctx N]
                    [-ctk|--cache-type-k T] [-ctv|--cache-type-v T] [--timeout S]
                    [--dry-run]
```

- `ENTRY` is a local-registry entry name, which names the LLM and the quant,
  e.g. `bartowski-k2-horizon-32b-q4-k-m`. The model must be installed.
- **stdout** is a single number: decimal GB (1e9 bytes), one decimal place,
  **rounded up**. That's the unit of `min_memory`/`memory`.
- **stderr** carries the launched command, a MiB breakdown (weights, KV and
  recurrent state, compute, output), the prompt-cache note and the GPU
  warning.
- Exit code `0` means it measured or projected; `1` means it failed (unknown
  or missing model, llama.cpp not installed, llama-server exited before
  loading, the timeout ran out, 600 s by default, or `--dry-run` got no
  projection).

## Models too large to load: the `llama-fit-params` projection

Before loading anything, `kodo-memcheck` runs `llama-fit-params`, which
ships next to `llama-server` in every llama.cpp build, with the same flags.
That tool builds the model and context without allocating anything and
reads only the GGUF metadata. An 80.4 GiB GGUF took 0.16 s, peaked at
237 MiB of RAM and did no disk reads. Its first `memory breakdown` table is
the projection for the flags as given; the later tables are its own attempts
to move layers off the GPU, and they're ignored.

- When the projected total is **more than this machine's physical RAM**, the
  projection is reported instead of loading, and stderr says so.
- With **`--dry-run`**, the projection is always reported and nothing is
  loaded. It fails with exit `1` if the build has no `llama-fit-params` or it
  prints no table.
- Otherwise the model is loaded and measured as below. If `llama-fit-params`
  is missing or fails, stderr notes it and the model is loaded without the
  pre-check.

`llama-fit-params` rejects server-only flags (`--jinja`,
`--reasoning-format`, `--reasoning-budget`, `--alias`, `--cache-ram`) and the
speculative-decoding ones (`--spec-type`, `--model-draft`). Each rejected flag
is dropped and the run retried, and stderr lists what was left out. Dropping
a speculative flag gets a warning, because the projection then has **no MTP
draft context**: it can't model one, and the draft takes about 1 GB at 128K
on Ornith 35B.

Accuracy (b11552, M5 Max) against a real load with double-counted weights
removed: +48 MiB on Nanbeige 3B at 32K, +29 MiB on K2 Horizon BF16 at 4K, and
+280 MiB (0.2%) at 256K. The KV cache and device compute match exactly, and
the weights come from tensor sizes, so the projection never double-counts
them. It overstates host compute buffers by about 2x, so it always errs a
little high. There's no output-buffer row, so `output` reads 0.

## What gets launched

The launch flags are the entry's **catalog defaults**: its base args plus
each knob's catalog default (`knob_defaults`, else the knob's own default).
The user's stored knob picks and user-defined profiles are ignored on purpose
so the result is reproducible and describes what the catalog ships. The
command-line overrides go on top:

| Option | Effect |
| --- | --- |
| `--ctx-size N` | Replaces `--ctx-size` (`0` = the GGUF's trained length). |
| `--rope-scale F`, `--yarn-orig-ctx N` | Set those flags and imply `--rope-scaling yarn`. YaRN doesn't change allocations; it's passed so the launch is the one you'd actually run. |
| `--cache-type-k/-v T` | KV cache element type (`f16`, `q8_0`, `q4_0`, …). |

A stale rope flag from the catalog default stays in place when only
`--ctx-size` is overridden. That's harmless, because rope scaling doesn't
affect memory.

The probe adds two flags of its own:

- `-lv 4` goes **first**. Buffer sizes are printed only at verbosity 4.
  llama-server applies flags in order, and a device-touching flag such as
  `--n-gpu-layers` initializes the backends while the flags are still being
  parsed. A later `-lv` would miss the backend's own init lines, including
  Metal's `has unified memory = true`. That's how the GPU warning once fired
  on Apple Silicon.
- `--fit off` goes last. Without it, llama.cpp may quietly shrink the context
  to fit the free memory, and the measurement would describe a smaller
  configuration than the one requested.

llama-server binds a free loopback port and is stopped as soon as it logs
`model loaded`. No request is ever sent, because every buffer exists by then:
the KV cache is allocated for the full `--ctx-size`, and compute buffers are
reserved for the worst-case batch.

## What is counted

The sum of every `… buffer size = X MiB` line on every backend:

- **weights**: `load_tensors: … model buffer size`, covering every shard and
  any draft model. Buffers loaded with mmap (`*_Mapped`) are views of the
  GGUF file's pages, and they can overlap. On Apple Silicon a model's Metal
  buffers can span the whole file, including the token embedding that is
  also reported as the `CPU_Mapped` buffer. For K2 Horizon 32B BF16 that's
  2447.5 + 66337.5 MiB of buffers over a 66348.5 MiB file. Overlapping pages
  are resident only once, so the mapped buffers count as at most the total
  size of the GGUF files loaded (all shards of a split GGUF, plus a
  `--model-draft` head). When this cap applies, a stderr note gives the
  overlap. llama.cpp's own breakdown table counts the overlap twice.
- **kv/state**: `KV buffer size` and `RS buffer size` (recurrent state) of
  every context.
- **compute**: `compute buffer size` of every context. A context that is
  re-reserved (llama-server re-reserves the MTP draft context once
  speculative decoding is wired up) counts once; the later reservation
  replaces the earlier one. Contexts are delimited by
  `constructing llama_context`.
- **output**: `output buffer size` of every context.

The `common_memory_breakdown_print` table that llama-server prints on
shutdown is deliberately **not** used. It covers only the main context, so an
MTP draft context's KV and compute buffers land in its `unaccounted` column,
and that column also absorbs other processes' use of the device.

**Not counted:** llama-server's host-RAM prompt cache. It isn't allocated at
load; it grows at runtime up to `--cache-ram`, which is 8192 MiB by default
and isn't set by kodo. When it's enabled, a stderr note gives the limit.

## Unified memory only

On a unified-memory machine (Apple Silicon, or an APU/NPU sharing system
RAM), the sum is the model's memory requirement. On a discrete GPU the sum
still prints, but it mixes VRAM and system RAM. A stderr warning then says
the figure isn't meaningful. The warning fires when buffers land on a
non-host backend whose log didn't declare unified memory:

- Metal declares it with `has unified memory = true`.
- Vulkan declares it with `uma: 1`.
- CUDA, ROCm and SYCL never declare it, so an integrated CUDA device (e.g.
  GB10) gets a false warning.
- A CPU-only build never warns.

Host backends are `CPU*` buffers and `*_Host` (pinned host RAM).

## Layering

`kodo.memcheck` imports `kodo.llms` and `kodo.project` only, and nothing in
kodo imports it ([INTERNALS.md](INTERNALS.md)). It resolves the GGUF with
`find_installed_model_path`, which only reads state and never pauses a
download in progress. The binary is the llama-server override path when set,
else the installed build.
