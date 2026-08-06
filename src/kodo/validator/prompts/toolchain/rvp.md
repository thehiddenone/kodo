# Result Validation — the Fibonacci CLI calculator, its tests, and its toolchain

The assistant under review was asked to build a **command-line Fibonacci
number calculator**, with automated tests and a working project toolchain, in
a target language named in its exact instructions (the task-prompt section of
your assignment). The questions it asked and the answers it received, if any,
are in the interaction log. Grade what it actually delivered against the
intended design below **and** against how it was told to work.

## The intended design

- A **CLI utility** in the target language, using that language's normal
  idioms and standard project layout.
- **Invocation.** Run with the index `N` as a single command-line argument
  (e.g. `fib 10`) — no interactive prompt loop. It prints the Nth Fibonacci
  number and exits.
- **Sequence convention: 1-indexed.** `F(1) = 1`, `F(2) = 1`,
  `F(n) = F(n-1) + F(n-2)` for `n > 2` (1, 1, 2, 3, 5, 8, 13, …).
- **Argument validation:** a missing argument, a non-integer argument, or
  `N < 1` is rejected with a clear error message and a non-zero exit status —
  the program never crashes or dumps a raw stack trace/exception on bad
  input.
- **Ordinary integer types are sufficient.** No bignum/arbitrary-precision
  requirement, and overflow behavior for very large `N` is not part of the
  spec.
- **Automated tests** covering correct output for several values of `N`
  (including the base cases `N=1` and `N=2`, and larger values exercising the
  recurrence) and rejection of invalid input (missing, non-integer, `N < 1`).
- **A working project toolchain**: build, format, static-analysis, and test
  scripts for the target language, wired up so the project can be built and
  tested repeatably from the command line.

Where the interaction log settled one of these choices, the confirmed answer
there is the source of truth for it; where the log is silent or empty, the
task prompt is.

## What NOT to grade

**Do not grade big-number/overflow behavior.** The task explicitly said
ordinary integer types are fine and overflow on very large `N` is not
required — don't dock points for a fixed-width integer overflowing or
wrapping on inputs large enough to exceed it; only dock points if the program
crashes (rather than producing *some* output, even an incorrect one) on such
input. **Do not grade exact error-message wording** or exit-code value, only
that invalid input is rejected clearly and without a crash. If, and only if,
the task prompt you were given explicitly requires arbitrary-precision output,
that becomes a real requirement — but the shipped task for this scenario does
not, so this should not apply here.

## How to check the toolchain and tests: use `toolchain_build`, don't just read

Unlike a plain read-only review, you have the **`toolchain_build`** tool for
this scenario, in addition to your usual read tools. Use it — don't just
infer from reading the scripts whether they would work.

1. Find the workspace root path for this run in the "Workspace under
   evaluation" section below.
2. Call `toolchain_build` with that path as `project_path`, using its default
   steps (`build`, `static_analysis`, `test` all on; `format` off — you don't
   need to mutate the source to judge it). This actually builds the project
   and runs its real test suite through the scripts the assistant set up, and
   returns per-step success and output logs (build errors, lint findings,
   test failures with stack traces) — treat this real, executed output as
   your primary evidence for "does it build," "does it work," and "do the
   tests pass," not a guess from eyeballing the code.
3. If the tool reports no `scripts/` toolchain exists at all (a "no script
   found" result), that **is** the finding — the toolchain requirement was not
   delivered — score it as a missing-required-feature/does-not-build
   situation, whichever the report's own scale calls closer; don't try to
   build or run anything yourself by other means.
4. Read through the failure logs `toolchain_build` returns for any step that
   failed, so your report can cite the actual error or failing assertion, not
   just "it failed."
5. **Test coverage** here means qualitative coverage, not a numeric percentage
   the toolchain won't produce on its own: read the test file(s) yourself and
   check whether they actually exercise correct output across several values
   of `N` (not just one), the base cases, and invalid-input rejection — not
   merely whether the `test` step passed. A test step that passes because the
   tests are trivial or barely touch the logic is still a coverage gap worth
   noting and deducting for.
6. You still have no general command-execution tool and no editing tools —
   `toolchain_build` is the one narrow exception, scoped to running the
   project's own generated scripts. Don't try to invoke anything else.

## What to check, end to end

1. **Project & toolchain exist and build.** A real project in the target
   language's standard layout, with a `scripts/` toolchain that
   `toolchain_build` can actually run.
2. **It builds and its tests pass** — per the real `toolchain_build` output,
   not a guess.
3. **Correctness.** Spot-check the implementation (or its test assertions)
   against the 1-indexed convention: `F(1)=1`, `F(2)=1`, `F(3)=2`, `F(4)=3`,
   `F(5)=5`, `F(6)=8`, `F(7)=13`, and a couple of larger values.
4. **Invocation.** The CLI takes `N` as a command-line argument and prints
   just the result — no interactive prompting for `N`.
5. **Argument validation.** A missing argument, a non-integer argument, and
   `N < 1` are each rejected with a clear message and a non-zero exit,
   without a crash (see "What NOT to grade" above for what's out of scope
   here).
6. **Tests.** Present, passing (per `toolchain_build`), and — per the
   qualitative check above — actually exercising correct output and
   invalid-input rejection rather than just existing as a formality.
7. **Conduct.** The task told the assistant to just build, without asking
   first. An empty interaction log is correct here; needless back-and-forth
   or a "you decide" hand-back on something the task already settled
   (invocation style, indexing convention, integer-size leniency, toolchain
   requirement) is the fault instead.
8. **Quality.** Readable, coherent, idiomatic for the target language, with
   no obvious bugs beyond what `toolchain_build` already surfaced.
