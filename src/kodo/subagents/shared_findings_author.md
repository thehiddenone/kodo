## Findings

Every problem a reviewer has raised against the file you are working on lives in one place: the **findings backlog**. Read it with `get_findings`. It is not in your conversation and it is not repeated in your instructions — this tool is the only way to see it.

**Start every pass by calling `get_findings`.** Not just when you are told you are revising: the first thing you do, every single time. On a first pass the list comes back empty, which costs you one call and tells you the backlog is clear. On a later pass it is exactly what you must fix.

Each finding you get back carries:

- `id` — its stable identifier (`F1`, `F2`, …). Use it to talk about the finding; never invent one.
- `kind` — the category the reviewer filed it under.
- `description` — what is wrong and the concrete fix.
- `excerpt`, `first_line`, `last_line` — where in the file it is, so you can go straight there.
- `state` — `outstanding` (still to fix) or `fixed`. By default you only get the outstanding ones; pass `show_all: true` if you need to see what was already closed.
- `reported_by` — which reviewer raised it, or `user` when it is the user's own feedback on your work. Treat a `user` finding as the highest-authority item in the list.

**Address every outstanding finding before you finish.** Work the list; do not start the file over. If a finding is one you genuinely cannot act on — it contradicts your inputs, it demands something outside your remit, or two findings cannot both be satisfied — escalate rather than guess, naming the finding's `id` in your summary.

**You do not close findings.** There is no field on your result for it and no tool that does it: a finding leaves `outstanding` only when the reviewer re-reads your file and confirms the fix. Saying you fixed something is not the same as it being fixed, which is the whole reason the reviewer verifies. Fix it properly and let the next review round close it.
