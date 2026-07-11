Build me a **Tic Tac Toe game that runs in the console and reads player moves
from standard input**, in the current workspace. I've thought it through, so
please just implement it to this spec — no need to ask me anything first.

**What to build**

- **Language / footprint:** Python 3, standard library only (no third-party
  packages). One self-contained, runnable script.
- **File:** create it as `tictactoe.py` in the workspace. Actually write the
  file with your tools — don't just print the code in the chat.
- **Players:** two humans taking turns at the same keyboard (hot-seat), `X`
  and `O`. **X moves first.** No computer opponent.
- **Board:** a 3×3 grid drawn to the console and re-drawn after every move.
  Number the cells **1–9 like a phone keypad** — 1 = top-left, 2 = top-middle,
  3 = top-right, … 9 = bottom-right — and make the numbering clear to players
  (e.g. show it on empty cells or print a small legend).
- **Input:** each turn, prompt the current player and read their move from
  stdin as a **single cell number 1–9**.
- **Validation:** reject input that isn't a number, is outside 1–9, or names a
  cell that's already taken. On bad input, print a short, friendly message and
  ask again — **never crash**.
- **Game end:** detect a win on any of the eight lines (3 rows, 3 columns, 2
  diagonals) and a draw (board full, no winner); announce the result clearly
  and stop the round.
- **Replay:** after a round ends, ask **"Play again? (y/n)"** — on `y` start a
  fresh game, on `n` exit cleanly.

When you're done, tell me the filename and the exact command to run it.
