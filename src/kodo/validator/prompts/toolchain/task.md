Build a **command-line Fibonacci number calculator**, in the current
workspace, using **{language}**.

I've thought this through, so please just implement it to this spec — no need
to ask me anything first.

**What to build**

- **Language:** {language}, using that ecosystem's normal idioms and standard
  project layout. Don't invent a nonstandard structure — set the project up
  the way a {language} developer would.
- **Interface:** a CLI utility invoked with the index `N` as a single
  command-line argument (e.g. `fib 10`) — no interactive prompt loop. It
  computes the Nth Fibonacci number, prints it to stdout, and exits.
- **Sequence convention:** 1-indexed. `F(1) = 1`, `F(2) = 1`, and
  `F(n) = F(n-1) + F(n-2)` for `n > 2` (so the sequence is 1, 1, 2, 3, 5, 8,
  13, …).
- **Argument validation:** if the argument is missing, isn't an integer, or is
  less than 1, print a short, clear error message and exit with a non-zero
  status — the program must never crash or print a raw stack trace/exception
  on bad input.
- **Integer size:** ordinary/native integer types are fine. You don't need to
  use a bignum or arbitrary-precision library, and overflow behavior for very
  large `N` is not a requirement.
- **Tests:** write automated tests covering: correct output for several values
  of `N` (including the base cases `N=1` and `N=2`, and at least a few larger
  values that exercise the recurrence), and rejection of invalid input
  (missing argument, non-integer argument, `N < 1`) — using {language}'s
  standard or most widely-adopted testing tool.
- **Toolchain:** set up the project's build toolchain — the standard build,
  format, static-analysis, and test scripts for {language} — so the
  calculator and its tests can be built and run repeatably from the command
  line, not just from inside an editor.

When you're done, verify the toolchain actually works yourself — build the
project and run the tests — and then tell me the exact commands to build the
calculator, run it (with an example), and run its tests.
