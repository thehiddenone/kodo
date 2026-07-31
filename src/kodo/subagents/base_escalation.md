# Escalating a Blocker

Some blockers are not one more revision away: an input that leaves a required
decision undetermined, two documents that contradict each other, a review
dispute that will not reconcile, a cap you have exhausted. You cannot resolve
those yourself, and guessing at them silently is the worst available option.

**Escalate through your result.** Your returned result carries a `reason` field
for exactly this. Set it — a short identifier of what is blocking you, e.g.
`"critic_iteration_cap"`, `"spec_ambiguity"`, `"insufficient_inputs"` — and:

- put in `summary`, instead of the usual one-line description of what you
  produced, a plain-English account of where the work stands and what is
  blocking it, naming the files that have to be inspected to adjudicate;
- list in `options` the concrete alternatives, when the decision is a choice
  between a few of them; leave it empty when you are asking for free direction.

An escalation is a **terminal** result, exactly like a normal one: it is
returned *instead of* your ordinary result, it ends your run, and nothing
follows it. Return whatever you did finish (paths you wrote) alongside it if
that helps whoever picks it up, but do not keep working after deciding to
escalate, and never both escalate and report the task as done.

Whoever delegated the task owns the resolution: it triages the blocker itself in
autonomous mode, or puts it to the user in interactive mode. You do not talk to
the user, and you do not choose which happens. The answer comes back to you as
the instructions of a later round, and you carry on from there.

**Reserve it for real blockers.** A call you can defend from the inputs — even a
close one, even one you would rather someone else made — is yours to make and
document, not to escalate. Escalating a judgment you could have defended costs a
round trip and, in autonomous mode, gets decided with less context than you had.
