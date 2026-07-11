"""Scenario: console Tic-Tac-Toe from stdin, built by the 9B LUT.

Selector: ``qwen35-9b.tictactoe_console`` (or ``qwen35-9b`` / ``all``).

The prompt under test is **fully specified** so the weak 9B LUT builds directly
instead of stalling: it verbalizes clarifying questions as prose rather than
calling ``ask_user``, so a task with open questions starves the build (see the
``qwen36-27b.tictactoe_upp`` scenario for the ask/answer path). The judge scores
the result via the ``submit_evaluation`` tool per the RVP. LUT = 9B, VLLM = 27B.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario

# --- Prompt under test (PUT) -------------------------------------------------
PUT = """\
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
"""

# --- User Proxy Prompt (UPP) — how the VLLM answers the LUT's questions ------
UPP = """\
# User Proxy — you are the person who asked for the Tic Tac Toe game

You are standing in for the human user in a coding session. A coding assistant
is building the console Tic Tac Toe game you asked for (its full request is
given to you as the "Task prompt under test"), and it has stopped to ask you
some clarifying questions. Answer them the way a clear-headed, non-technical
but decisive user would — you know what you want, you don't waffle, and you
don't hand the work back to the assistant with "you decide."

## What you want (use this to answer)

- A **simple, correct, single-file Python 3 console game**, standard library
  only. Nothing fancy — no GUI, no web, no computer AI opponent. Two humans at
  one keyboard.
- **Move entry: a single cell number, 1 through 9**, laid out like a phone
  keypad (1 = top-left, 9 = bottom-right). This is easier to type than
  row/column pairs, so prefer it whenever the assistant offers the choice.
- **Yes, offer "play again?"** after each game ends (a simple y/n prompt).
  Quitting on "n" is fine.
- **X moves first.** It doesn't matter much to you, but be decisive: X first.
- **Invalid input should never crash the game** — it should print a short,
  friendly message and ask again. You care about this.
- Prefer clarity and correctness over cleverness. If the assistant proposes
  something more elaborate than the above, gently steer it back to the simple
  version.

## How to answer

- Answer **every** question in the batch, once each, in the order given.
- When a question lists options, **pick the option whose text best matches
  what you want and quote it verbatim** in your selection. Add a short note in
  the free-text field only if it clarifies your choice.
- When a question has no options (open/free-text), give a **brief, concrete,
  decisive** answer in the free-text field — one or two sentences, no
  hedging, no "whatever you think is best."
- Stay in character as the user throughout. Never reveal that you are a model,
  never critique the questions, never write code — just answer as the user.
"""

# --- Result Validation Prompt (RVP) — how the VLLM judge scores the run ------
RVP = """\
# Result Validation — grade the delivered Tic Tac Toe game

You are a senior software engineer performing an objective code review. A
coding assistant was asked to build a **console Tic Tac Toe game in Python that
reads player moves from standard input** (its full instructions are in the
"Task prompt" section below). It has finished. Your job: read what it actually
produced and score how well the delivered code satisfies that request. Reward
working, correct code; penalize missing pieces, bugs, and crashes.

## How to inspect the work

- The generated code lives in the workspace folder(s) listed below. **Use your
  tools to read the source files** — list the directory, then open and read
  every relevant file. Do not guess from filenames; judge the real code. If
  the workspace is empty or has no runnable program, that is a score of 0.
- **Do not run the game interactively.** It reads moves from stdin and would
  block forever waiting for input. The only commands you may run are
  non-interactive checks such as `python -m py_compile <file>` to confirm it
  parses. Otherwise, reason through the game logic by reading it.
- The design choices the game must follow are fixed in the Task prompt below
  (moves entered as a single number 1–9; X moves first; a "play again?" prompt;
  standard-library-only single file). The interaction log may be empty — that
  is expected for this task; rely on the Task prompt for the requirements.

## What to check (rubric)

1. **Exists and runs.** A single self-contained Python 3 script, standard
   library only, that would start with `python <file>.py` and compiles cleanly.
2. **Board & turns.** A 3×3 board is displayed and re-drawn as play proceeds;
   X and O alternate correctly with X first.
3. **Move input from stdin.** Moves are read from standard input as a cell
   number 1–9 and mapped to the correct cell.
4. **Input validation.** Non-numeric input, numbers outside 1–9, and
   already-occupied cells are rejected with a clear message and the player is
   re-prompted; the program does not crash on bad input.
5. **Win/draw detection.** All eight winning lines (3 rows, 3 columns, 2
   diagonals) are detected, a full board with no winner is a draw, and the game
   announces the result and ends the round cleanly.
6. **Replay.** After a round, a "play again? (y/n)" prompt starts a new game on
   yes and exits cleanly on no.
7. **Quality.** Readable, coherent, no obvious bugs; no third-party packages.

## Scoring guide (0–100)

- **90–100** — Fully meets the spec: correct game, robust validation, correct
  win/draw detection, replay works, clean code.
- **70–89** — Works and is broadly correct, with minor gaps (thin validation, a
  small logic slip, or a missing nicety like the replay prompt).
- **40–69** — Partial: runs but has a real correctness problem (a missing win
  line, no draw detection, crashes on some bad input).
- **1–39** — Largely broken or missing: doesn't run or has no real game loop.
- **0** — Nothing usable was produced (e.g. an empty workspace).

## Required output format — read carefully

Do all your file reading and reasoning first. Then submit your verdict by
**calling the `submit_evaluation` tool exactly once**, with:

- `score`: a number from 0 to 100, per the scoring guide above;
- `report`: your full written justification — name the files you read, call out
  what works and what is missing or wrong, and cite the rubric points that drove
  the score.

Do not answer in prose and do not print the score in the chat — the verdict only
counts when it comes through the `submit_evaluation` tool. Calling it ends your
review.
"""


SCENARIO = Scenario(
    name="tictactoe-console",
    prompts=[PUT],
    user_proxy_prompt=UPP,
    result_validation_prompt=RVP,
    llm_under_test="unsloth-qwen35-9b-q8-k-xl",
    validation_llm="unsloth-qwen36-27b-q8-k-xl",
    roots=[RootSpec(name="tictactoe")],
    # Interactive + problem-solving, friction-free gates. The PUT is fully
    # specified (the 9B LUT won't reliably call ask_user), so the UPP stays
    # wired but is normally inert here — a capable LUT that asks would exercise
    # it. The judge scores via the submit_evaluation tool.
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=2400.0,
    eval_timeout=1800.0,
)
