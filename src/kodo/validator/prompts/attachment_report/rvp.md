# Result Validation — attachment-driven analysis, tests, toolchain, and report

The assistant under review was given a task in **two turns**:

1. An almost-empty prompt plus an **attached `spec.md`**, which held the entire
   specification: write `analyze.py` (reads `data.csv` → writes
   `results.json`), write `pytest` unit tests for its logic, and set up a build
   toolchain. The attachment was the only place the requirements existed.
2. A follow-up asking it to read `results.json` **from disk** and write
   `report.md` classifying each region against the global mean.

Grade what it actually delivered, and how it worked.

## The expected numbers

`data.csv` is fixed, so the correct output is known exactly. Per-region totals
(sum of `units × unit_price`):

| Region | Total |
|---|---|
| north | 758.00 |
| south | 925.50 |
| east | 677.50 |
| west | 997.50 |
| central | 1239.75 |

`global_mean` = **919.65** (4598.25 ÷ 5).

Therefore `report.md` must classify:

- **Above average** (> 919.65): **south**, **west**, **central**
- **Below average**: **north**, **east**

Accept tiny floating-point differences (±0.01) in the totals. Do **not** accept
a different *classification* — the split above is the correct one.

Note that the mean of the region totals (919.65) is not the same as the mean of
the individual CSV rows. If a submission reports a `global_mean` that is not
~919.65, check which of the two it computed; averaging the rows instead of the
region totals is a real spec violation, not a rounding difference.

## Gate 1 — did it read the attachment? (score 0 if not)

**This is the single most important check, and it is pass/fail.**

The specification existed *only* in the attached file. To read it the assistant
had to call the **`read_attachment`** tool with the attachment's `attachment_id`
— a UUID it must reproduce **exactly** from the `<ATTACHMENT ID="…"/>` tag in
its context.

Check the interaction log and the workspace for evidence:

- If `read_attachment` was never called successfully, or the assistant claimed
  it could not read the attachment, or it invented requirements instead of
  reading them — **the score is 0**, regardless of what else it built. A
  plausible-looking `analyze.py` written from a guess about what the spec
  probably said is a **failure**, not partial credit.
- Repeated failed `read_attachment` calls with a malformed or wrong
  `attachment_id` are the signature of this failing. Call that out explicitly
  in your report if you see it: it means the model could not reproduce the
  UUID, which is exactly what this scenario exists to detect.

Everything below is only worth grading if this gate passed.

## Gate 2 — did it read `results.json` for the report? (heavy deduction if not)

The second turn explicitly said to read `results.json` from disk rather than
recompute or recall. Look for a `read_file` on `results.json` in the second
turn. A report whose numbers happen to be right but that was never read from
the file has not done the task; a report with numbers that do **not** match
`results.json` is worse — it means the assistant made up figures.

## How to check the toolchain and tests: use `toolchain_build`, don't just read

You have the **`toolchain_build`** tool for this scenario in addition to your
read tools. Use it — don't infer from reading the scripts whether they work.

1. Find the workspace root path in the "Workspace under evaluation" section.
2. Call `toolchain_build` with that path as `project_path`, default steps
   (`build`, `static_analysis`, `test` on; `format` off). This runs the real
   scripts the assistant set up and returns per-step success plus output logs.
   Treat that executed output as your primary evidence, not a guess.
3. If it reports no `scripts/` toolchain exists at all, that **is** the finding
   — the toolchain requirement was not delivered.
4. Quote the actual failing assertion or error from the logs for any failed
   step; don't just write "it failed".
5. **Test quality is qualitative** — read the test files yourself. The spec
   named five things to cover: per-region summation, grouping across multiple
   regions, the mean being over region *totals* (not rows), rounding to 2
   decimals, and an edge case. Check whether the tests genuinely exercise those
   rather than merely existing. The spec also required tests to use their own
   fixture data rather than the real `data.csv` — a test suite that reads the
   real input file is a real deduction, since it silently couples the tests to
   the very data the tool is meant to process.
6. You have no general command-execution tool and no editing tools —
   `toolchain_build` is the one narrow exception. Don't try to invoke anything
   else.

## How it was expected to work

This task is deliberately multi-part (analysis code + tests + toolchain), so its
shape is not something Problem Solver can know up front — it should delegate
rather than work directly. A well-run session should show:

- a **`planner`** invocation producing an ordered plan,
- a **`toolchain_builder`** invocation setting up the Python toolchain, and
- **`developer`** invocation(s) doing the coding.

Treat this as a **conduct signal, not a hard requirement**: a run that
delivered everything correctly by another route is still a good run, and you
should say so. But a run that skipped planning and produced a disorganised or
incomplete delivery should have that noted as a contributing cause. Do not
reward a submission for merely *mentioning* these sub-agents in prose without
actually delegating.

## What to check, end to end

1. **The attachment was read** (Gate 1 — pass/fail, above).
2. **`analyze.py` exists**, reads `data.csv`, and computes revenue as
   `units × unit_price` grouped by region. Region names and values are read
   from the CSV, not hardcoded.
3. **`results.json` exists** with the `regions` / `global_mean` shape the spec
   named and the correct values (±0.01).
4. **The logic is importable and tested**, not buried in a `__main__` block.
5. **Tests exist and pass** per `toolchain_build`, and genuinely cover the five
   named areas using their own fixture data.
6. **A working toolchain exists** — build/bootstrap, format, static-analysis
   and test scripts that `toolchain_build` can actually run.
7. **`report.md` exists**, was written from `results.json` (Gate 2), states the
   global mean, and splits the regions into exactly {south, west, central}
   above and {north, east} below, with the exact recommendation strings
   `INCREASE STOCK` and `REVIEW PRICING` respectively.
8. **Conduct.** The task said to implement without asking first. An empty
   interaction log is correct here; needless back-and-forth, or handing a
   decision back that the spec already settled, is a fault. Note whether
   planning/toolchain/developer delegation happened (above).
9. **Quality.** Readable, idiomatic Python, standard project layout, no
   obvious bugs beyond what `toolchain_build` surfaced.

## Scoring guidance

- Gate 1 failed → **0**.
- Gate 1 passed but no `results.json`, or its numbers are wrong → cap at **35**.
- Correct `results.json` but no tests **or** no toolchain → cap at **60**.
- Everything delivered but `report.md` misclassifies a region, or its numbers
  disagree with `results.json` → cap at **75**.
- Everything correct, tests meaningful, toolchain runs clean → **85–100**,
  using the top of the range only for genuinely idiomatic, well-structured work.
