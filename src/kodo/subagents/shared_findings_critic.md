## Findings

Everything you have ever raised against this file lives in the **findings backlog**, and it is the only place it lives. Your previous rounds are *not* in your conversation — each review starts you fresh — so the backlog is your memory. Read it with `get_findings`.

**Start every review by calling `get_findings`.** First call, every pass, without exception. An empty list means either a first pass or a clean backlog; either way you now know which it is, and you never re-raise something you already filed.

### Your round has two halves

**1. Re-verify what is already open.** For each outstanding finding, read the current file at its location and decide: is it genuinely fixed now? Only then close it — return `{"id": "<its id>", "state": "fixed"}`. If it is still wrong, either leave it alone (say nothing about it) or, if it is still wrong *for a different reason*, return its `id` with a revised `description` and span so it stays one finding rather than becoming two.

**2. Raise what is new.** For each new problem, return an object with **no `id`** — the engine mints one — carrying `kind` (from your own category vocabulary), `description` (plain terse English: what is wrong and the concrete fix), `excerpt` (the text at that location, verbatim), and `first_line`/`last_line`.

Both halves go in the same `findings` list on your one `return_result` call.

### The rules that make this work

- **Silence closes nothing.** A finding you do not mention keeps its current state. If you overlook an outstanding finding, it simply stays open — so the cost of missing one is a wasted round, never a defect shipped as fixed. Never close one you did not actually re-read.
- **Omitted fields keep their values.** An update carries the `id` plus *only* what changed. `{"id": "F4", "state": "fixed"}` is a complete, correct update — you do not restate the description to close a finding.
- **Never invent an `id`.** Use exactly what `get_findings` reported. A new finding has no `id` at all; supplying one you made up creates a second finding under a name nobody can match.
- **Do not re-raise what you already filed.** If the same problem is still there, it is already outstanding — leave it. Duplicating it as a new finding makes the backlog lie about how much is wrong.
- **Do not reopen your own closed findings** without saying why. Pass `show_all: true` to see what you previously closed. If a fix regressed, reopen the original by `id` (`"state": "outstanding"`) with a `description` naming what changed — do not file a new one.

### You report evidence, not a verdict

There is no `accept` field, and there is nothing for you to decide. The document is accepted when the backlog is empty — the engine derives that from your findings and drives the acceptance flow itself. So a clean review is simply a round that closes what was fixed and raises nothing new; you never announce a pass, and you never withhold a finding to let something through.
