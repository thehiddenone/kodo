# Validation suite summary

You are reviewing the results of a validation suite: several LLMs (each
possibly a different local model, or the same model under a different
sampling flavor) were each run against one or more scenarios, and each
run was already scored and reported on by a judge. You will be given every
entry's report — the LLM+flavor and scenario it covers, its score (when one
was produced), and the judge's full written assessment — and nothing else.
Do not assume you have access to any tools; you do not, and none of the
entries' underlying workspaces are available to you here. Work only from the
reports you are given.

Produce one **detailed, comparative** summary covering every LLM the suite
validated. Structure it as:

1. **Overview** — one short paragraph: how many LLMs/entries were covered,
   and the headline result (who came out ahead, and by how much, if the
   scores make that clear).
2. **Per-LLM breakdown** — for each distinct LLM+flavor, a short paragraph
   synthesizing its entries: strengths, weaknesses, and any recurring
   failure pattern across its reports (not just repeating each report's own
   text).
3. **Cross-LLM comparison** — where two or more LLMs were run against the
   *same* scenario, compare their outcomes directly: which one handled it
   better and why, drawn from what the reports actually say.
4. **Notable patterns** — anything that shows up across several entries
   regardless of which LLM produced it (a scenario every LLM struggled with,
   a process failure — e.g. not asking when told to — that recurs, a sampling
   flavor that seems to help or hurt).
5. **Recommendation** — if the evidence supports it, which LLM(s) you would
   recommend for the kind of work this suite exercises, and any caveat worth
   flagging (e.g. only one run per LLM, so a single bad turn could be noise
   rather than a real weakness).

Ground every claim in the reports you were given — do not invent scores or
details no report actually stated. If an entry carries no score (its
scenario ran without a judge evaluation), say so plainly and reason about it
from whatever context its report does carry, rather than treating it as a
failure.

Reply with your full written summary in markdown prose. Do not call any tool.
