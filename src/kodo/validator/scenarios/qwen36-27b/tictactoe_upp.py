"""Scenario: exercise the User Proxy — underspecified task forces ``ask_user``.

Selector: ``qwen36-27b.tictactoe_upp`` (or ``qwen36-27b`` / ``all``).

Companion to ``qwen35-9b.tictactoe_console``. The PUT is **deliberately
underspecified** and tells the assistant to call ``ask_user`` before coding; to
make that reliable the LUT is the **same capable 27B model as the VLLM**, which
actually uses the structured ``ask_user`` tool. With LUT == VLLM the
``llm.select`` swaps around each answer are same-model no-ops, so the proxy
round-trip is just the grammar-constrained ``llm.complete``. Flow demonstrated:
LUT asks via ``ask_user`` → proxy answers per the UPP → LUT builds → judge
scores via the ``submit_evaluation`` tool per the RVP.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario

# --- Prompt under test (PUT) — deliberately underspecified -------------------
PUT = """\
I'd like a **Tic Tac Toe game I can play in the console**, written in Python.

I'm being deliberately vague here — I have opinions about the details but I've
left them out on purpose. **Before you write a single line of code, use the
`ask_user` tool to ask me the questions you need answered** to build it the way
I want. At a minimum I'd expect you to ask about:

- how a player enters a move,
- whether the game should let us play again after it ends,
- who takes the first turn.

Ask anything else you think matters too. Put them in one `ask_user` batch, wait
for my answers, and only then implement the game as a single standard-library
Python file in the workspace (write the file with your tools). When you're done,
tell me the filename and how to run it.
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
- The design choices come from the interaction log below — the questions the
  assistant asked and the answers it received (move entry, replay, first
  player, and so on). Judge the code against those confirmed choices as well as
  the Task prompt.

## What to check (rubric)

1. **Exists and runs.** A single self-contained Python 3 script, standard
   library only, that would start with `python <file>.py` and compiles cleanly.
2. **Board & turns.** A 3×3 board is displayed and re-drawn as play proceeds;
   the two players alternate correctly, with the first player matching the
   confirmed answer.
3. **Move input from stdin.** Moves are read from standard input in the format
   the user confirmed and mapped to the correct cell.
4. **Input validation.** Non-numeric input, out-of-range values, and
   already-occupied cells are rejected with a clear message and the player is
   re-prompted; the program does not crash on bad input.
5. **Win/draw detection.** All eight winning lines (3 rows, 3 columns, 2
   diagonals) are detected, a full board with no winner is a draw, and the game
   announces the result and ends the round cleanly.
6. **Follows the confirmed choices.** Matches the decisions in the interaction
   log (input scheme, replay behavior, first player).
7. **Quality.** Readable, coherent, no obvious bugs; no third-party packages.

## Scoring guide (0–100)

- **90–100** — Fully meets the request and the confirmed choices: correct game,
  robust validation, correct win/draw detection, clean code.
- **70–89** — Works and is broadly correct, with minor gaps (thin validation, a
  small logic slip, or a cosmetic mismatch with a confirmed choice).
- **40–69** — Partial: runs but has a real correctness problem, or ignores a
  confirmed choice.
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
    name="tictactoe-upp",
    prompts=[PUT],
    user_proxy_prompt=UPP,
    result_validation_prompt=RVP,
    # LUT == VLLM (the capable 27B): ask_user is reliably used and the proxy's
    # model swaps are no-ops.
    llm_under_test="unsloth-qwen36-27b-q8-k-xl",
    validation_llm="unsloth-qwen36-27b-q8-k-xl",
    roots=[RootSpec(name="tictactoe")],
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=2400.0,
    eval_timeout=1800.0,
)
