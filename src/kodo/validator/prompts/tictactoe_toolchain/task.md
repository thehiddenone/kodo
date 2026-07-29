Build a **command-line Tic-Tac-Toe game where a human plays against a computer
opponent**, in the current workspace, using **{language}**.

I've thought this through, so please just implement it to this spec — no need
to ask me anything first.

**What to build**

- **Language:** {language}, using that ecosystem's normal idioms and standard
  project layout. Don't invent a nonstandard structure — set the project up
  the way a {language} developer would.
- **Interface:** a CLI utility. The human runs it from a terminal, plays one
  full game, and the program exits when the game ends (a win, a loss, or a
  draw) — no replay loop is required.
- **Symbol choice:** at the start of the game, ask the human player (via the
  CLI — a simple prompt is fine) whether they want to play as X or O; the
  computer takes the other symbol.
- **Turn order:** X always moves first, regardless of whether X is the human
  or the computer. If the computer is playing X, it makes the first move
  without waiting for any human input.
- **Board:** a 3x3 grid printed to the console and re-printed after every
  move. Number the empty cells 1-9 like a phone keypad (1 = top-left, 2 =
  top-middle, 3 = top-right, … 9 = bottom-right) so the human knows what to
  type, and make the numbering visible on the board (e.g. show it on empty
  cells, or print a short legend).
- **Human input:** each human turn, read a single cell number 1-9 from the
  CLI. Reject input that isn't a number, is outside 1-9, or names an
  already-taken cell — print a short, clear message and re-prompt; the
  program must never crash on bad input.
- **Computer opponent:** the computer must always make a legal move (an empty
  cell) and must never be able to move onto an occupied cell. Beyond that, you
  choose the strategy and its strength yourself — no particular algorithm or
  difficulty level is required unless you judge one to be the natural,
  idiomatic choice for a project like this.
- **Game end:** detect a win on any of the eight lines (3 rows, 3 columns, 2
  diagonals) or a draw (the board fills with no winner), announce the result
  clearly, and exit.
- **Tests:** write automated tests covering the game's core logic — win
  detection on all eight lines, draw detection, turn order/alternation, and
  input validation/rejection — using {language}'s standard or most
  widely-adopted testing tool.
- **Toolchain:** set up the project's build toolchain — the standard build,
  format, static-analysis, and test scripts for {language} — so the game and
  its tests can be built and run repeatably from the command line, not just
  from inside an editor.

When you're done, verify the toolchain actually works yourself — build the
project and run the tests — and then tell me the exact commands to build the
game, run it, and run its tests.
