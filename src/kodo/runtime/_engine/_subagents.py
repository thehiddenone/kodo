"""Sub-agent dispatch: gated spawns, subsessions, and Author/Critic rounds.

Spawn permission is **not** wired to any one agent — there is no "only the
Guide spawns" assumption. Each agent declares the sub-agents it may spawn in
its frontmatter ``subagents:`` allow-list; the engine-driven agents
(:data:`~._shared._DIRECT_ONLY_AGENTS`) are never spawnable by anyone.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path, PurePosixPath

from kodo.common import Envelope
from kodo.findings import (
    STATE_OUTSTANDING,
    USER_FEEDBACK_REPORTER,
    RoundSummary,
    apply_findings,
    close_findings_for_paths,
    read_findings,
    sort_for_display,
)
from kodo.llms import Message
from kodo.subagents import (
    PHASE_INITIAL,
    PHASE_REVISION,
    PRODUCES_REMAINDER,
    RESPONSIBILITY_CODE_KEY,
    ROLE_ARCHITECTURE,
    AgentLoadError,
)
from kodo.tools import document_status, root_for
from kodo.toolspecs import MAX_ROUNDS_DEFAULT, SCHEMA_COMPLIANCE_KEY
from kodo.transport import (
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_SUBSESSION_ENDED,
    EVT_SUBSESSION_STARTED,
)
from kodo.workproducts import (
    WorkProduct,
    read_components,
    read_work_product,
    read_work_products,
    record_components,
    record_membership,
    resolve_needs,
    work_product_id,
)

from .._agenttools import agent_tool_specs
from ._proto import EngineHost
from ._shared import (
    _DEPSMGR_AGENT_NAME,
    _DIRECT_ONLY_AGENTS,
    _GUIDE_AGENT_NAME,
    _WEB_SEARCH_AGENT_NAME,
)

# Default web_search timeout when the tool's caller omits `timeout`, and the
# hard cap the tool itself already enforces before this is ever reached.
_DEFAULT_WEB_SEARCH_TIMEOUT_S = 180.0
_MAX_WEB_SEARCH_TIMEOUT_S = 600.0

# Author/critic rounds one ``run_subagent_<author>`` call may spend. The default
# is what the Guide's prompt used to tell it to budget by hand; the hard cap
# bounds a caller that asks for an absurd number, so a single tool call can never
# turn into an unbounded spend. A loop that is not converging usually stops well
# before either (see ``_run_review_loop``'s ``not_converging`` branch).
_DEFAULT_MAX_REVIEW_ROUNDS = MAX_ROUNDS_DEFAULT
_MAX_REVIEW_ROUNDS = 10

# Closing note appended to every schema-bearing spawn's rendered Input
# Parameters section (see ``_render_task_input``). This is the sub-agent's only
# remaining prose explanation of `return_result` beyond the tool's own
# `description` (`kodo.toolspecs._return_result._DESCRIPTION`) — the registry
# used to restate it inside a `## Your Task Contract` system-prompt section,
# which is gone; this per-call section is where it lives now.
_RETURN_RESULT_REMINDER = (
    "When you finish, call `return_result` exactly once. Its `result` "
    "parameter declares the exact shape you must produce — read it there and "
    "follow it exactly."
)

_log = logging.getLogger(__name__)


def _role_labels(role: str, paths: tuple[str, ...]) -> dict[str, str]:
    """``{label: path}`` for one resolved role, deterministically named.

    A single file is labelled with the bare role, which is what an agent's own
    contract calls it ("the architecture"). Several get ``<role>_<basename>``:
    far more readable in the rendered task than a numbered list, and it is the
    basename an agent will recognise. A basename clash inside one role falls
    back to a numeric suffix.
    """
    if not paths:
        return {}
    if len(paths) == 1:
        return {role: paths[0]}
    labels: dict[str, str] = {}
    for path in paths:
        base = PurePosixPath(path).name or path
        label = f"{role}_{base}"
        suffix = 2
        while label in labels:
            label = f"{role}_{base}_{suffix}"
            suffix += 1
        labels[label] = path
    return labels


def _reported_paths(output: dict[str, object]) -> list[str]:
    """Every path an author says it wrote this round — its whole work product.

    Replaced reading ``primary_path`` on 2026-09-04. Order is preserved because
    it is meaningful (the author is asked to list its entry point first, which
    is what the UI leads with); duplicates and non-strings are dropped, since
    this comes straight from a model.
    """
    raw = output.get("paths")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(p.strip() for p in raw if isinstance(p, str) and p.strip()))


def _produced_paths(spec: object, output: dict[str, object]) -> list[str]:
    """Fallback member set for an author that declares no ``paths`` field.

    ``narrative_author`` is the one pipeline author whose output schema predates
    the shared ``author_output`` shape: it reports ``narrative_path`` and
    ``tech_stack_path`` instead. Its work product is exactly those, read off the
    same ``produces`` map that assigns their roles, so nothing has to special-
    case the agent by name.
    """
    produces = getattr(spec, "produces", None)
    if not isinstance(produces, dict):
        return []
    paths: list[str] = []
    for field_name in produces.values():
        value = output.get(field_name)
        if isinstance(value, str) and value.strip() and value not in paths:
            paths.append(value)
    return paths


def _review_instructions(paths: tuple[str, ...]) -> str:
    """The critic's task line for one review round.

    Names every member file rather than one, and says plainly that they are a
    single change: a critic told to "review a.py" and separately handed b.py
    reviews two files, where the defect worth catching usually lives in how
    they fit together.
    """
    if len(paths) == 1:
        return f"Review {paths[0]}."
    listed = "\n".join(f"- {p}" for p in paths)
    return (
        "Review the following files. They are one change and must be judged "
        f"together, including how they fit with each other:\n{listed}"
    )


def _escalation_reason(output: dict[str, object]) -> str:
    """The blocker a sub-agent escalated in *output*, or ``""`` when it did not.

    A sub-agent escalates through its own ``return_result`` (there is no
    ``escalate_blocker`` tool any more — see ``specs/_shapes.py``), and the
    signal is a **non-empty ``reason``**: the field is optional and nullable, so
    a normal result simply omits it. Emptiness is what's tested, not presence —
    ``normalize_output`` backfills missing fields with ``""``, and a model that
    volunteers ``reason: ""`` on a good result is not escalating either.
    """
    reason = output.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


class SubagentMixin:
    """Gated sub-agent spawns, subsession lifecycle, and Author/Critic."""

    # Declared so the `= None` write in _spawn_subagent doesn't let mypy infer
    # a bare-None class attribute conflicting with the EngineHost/_core one.
    _replay_subsessions: list[dict[str, object]] | None

    # ------------------------------------------------------------------
    # Subagent dispatch
    # ------------------------------------------------------------------

    def _assert_can_spawn(self: EngineHost, caller: str, *names: str) -> None:
        """Gate a spawn: ``caller`` must be allowed to invoke every name in *names*.

        Permission is **not** wired to any one agent — there is no "only the
        Guide spawns" assumption. Each agent declares the sub-agents it may
        spawn in its frontmatter ``subagents:`` allow-list (see
        :meth:`AgentRegistry.allowed_subagents`); any agent that also holds a
        spawning tool can drive them. ``_DIRECT_ONLY_AGENTS`` (engine-driven
        agents such as the session titler) are never spawnable by anyone.

        Raises:
            PermissionError: ``caller`` may not spawn one of *names* — surfaced to
                the calling LLM as the tool's ``{"error": ...}`` result.
        """
        allowed = self._registry.allowed_subagents(caller)
        for name in names:
            if name in _DIRECT_ONLY_AGENTS:
                raise PermissionError(
                    f"{name!r} is engine-driven only and cannot be spawned as a sub-agent."
                )
            if name not in allowed:
                permitted = ", ".join(sorted(allowed)) or "(none)"
                raise PermissionError(
                    f"Agent {caller!r} is not permitted to spawn sub-agent {name!r}. "
                    f"Permitted sub-agents: {permitted}."
                )

    def _scoped_task_input(
        self: EngineHost, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Strip a ``responsibility_code`` the target agent has no business with.

        Only a **per-component** agent declares the field
        (:attr:`~kodo.subagents.SubAgentSpec.takes_responsibility_code`);
        stages 5-7 and nothing else. Aimed at any other agent it is not merely
        inert — it feeds the work-product id, so one stray code splits the
        record of a product-level stage across calls and every later stage
        asking for "this component's Functional Design" stops finding one.

        The field is left off those agents' schemas, so a caller is never
        offered it; this is the second half of the same guard, for the model
        that emits an undeclared key anyway. The engine owns which component a
        spawn is scoped to, exactly as it owns ``input_paths``
        (:data:`~kodo.toolspecs.ENGINE_OWNED_TASK_FIELDS`) — the rule used to
        live only in ``agent_guide.md``, where following it was the model's job.

        Dropping is silent to the caller (a warning in the log): the spawn is
        correct once the field is gone, so refusing it would spend a round on
        something the engine can simply fix.
        """
        if RESPONSIBILITY_CODE_KEY not in task_input or self._takes_responsibility(name):
            return task_input
        _log.warning(
            "dropping responsibility_code %r: %s is not a per-component agent, and a "
            "stray code would split its work product",
            task_input.get(RESPONSIBILITY_CODE_KEY),
            name,
        )
        return {k: v for k, v in task_input.items() if k != RESPONSIBILITY_CODE_KEY}

    def _takes_responsibility(self: EngineHost, name: str) -> bool:
        """Whether *name*'s spec declares ``responsibility_code`` (a per-component agent)."""
        spec = self._registry.spec_for(name)
        return spec is not None and spec.takes_responsibility_code

    def _responsibility_code(self: EngineHost, name: str, task_input: dict[str, object]) -> str:
        """The component *name*'s spawn is scoped to, or ``""``.

        Reads the task only for an agent whose spec declares the field, so every
        consumer of the code — the work-product id, ``self``/``dependencies``
        resolution — asks the spec rather than trusting what arrived
        (see :meth:`_scoped_task_input`).
        """
        if not self._takes_responsibility(name):
            return ""
        return str(task_input.get(RESPONSIBILITY_CODE_KEY) or "")

    async def _run_subagent(
        self: EngineHost,
        caller: str,
        name: str,
        task_input: dict[str, object],
        max_rounds: int | None = None,
    ) -> dict[str, object]:
        """Gate a caller's sub-agent spawn, then run it — with whatever review it declares.

        The shape is chosen by the *callee's* own frontmatter, never by the
        caller, from two independent flags:

        - ``critic:`` — a critic reviews every round.
        - ``user_review: true`` — the **user** signs the work product off at the
          approval gate before it is accepted.

        Either one makes the call a bounded loop (:meth:`_run_review_loop`)
        returning the agent's result plus a ``review`` block; neither makes it a
        single pass (:meth:`_run_unreviewed_author`). They compose: with both,
        critic rounds run first and the gate fires once the backlog is clear.

        The caller never names a critic, never asks for a gate, and never
        iterates by hand — which is the point of putting both declarations in
        the callee's frontmatter. Before ``user_review`` existed, "does a human
        sign this off?" was answered by whether the agent happened to have a
        critic, since the gate could only ever fire from the critic path.

        Args:
            caller: Agent making the call (the running agent — not assumed to be
                the Guide). Its frontmatter allow-list gates the spawn.
            name: Sub-agent name from the registry.
            task_input: Structured task, conforming to the sub-agent's
                ``input_schema``. Both spawn paths scope it first
                (:meth:`_scoped_task_input`), so a ``responsibility_code``
                aimed at an agent that is not per-component never reaches
                anything that reads it — the brief included.
            max_rounds: Caller's cap on review rounds, or ``None`` for
                :data:`_DEFAULT_MAX_REVIEW_ROUNDS`. Ignored when the sub-agent
                declares neither a critic nor a user review gate.

        Returns:
            dict: The sub-agent's structured result (its ``output_schema``),
            plus ``review`` when a review loop ran.

        Raises:
            PermissionError: ``caller`` is not permitted to spawn ``name`` (or,
                for a critic-reviewed sub-agent, its critic).
        """
        self._assert_can_spawn(caller, name)
        critic = self._critic_for(name)
        if not critic and not self._user_reviews(name):
            # Unreviewed authors still fill artifact roles — an author that
            # neither has a critic nor needs a sign-off still writes documents
            # every later stage resolves against — so they go through the same
            # resolve-then-record path as a reviewed one, just without the loop
            # around it.
            return await self._run_unreviewed_author(name, task_input)
        if critic:
            # The critic is spawned by the engine, but on this caller's behalf,
            # so it is gated against the same allow-list — a caller may not
            # reach a sub-agent it was never granted just because an author
            # points at it. A user gate needs no such check: the reviewer is a
            # person, not an agent.
            self._assert_can_spawn(caller, critic)
        return await self._run_review_loop(name, critic, task_input, max_rounds)

    def _critic_for(self: EngineHost, name: str) -> str:
        """The critic paired with sub-agent *name*, or ``""`` when it has none."""
        try:
            return self._registry.get(name).critic
        except AgentLoadError:
            return ""

    def _user_reviews(self: EngineHost, name: str) -> bool:
        """Whether sub-agent *name*'s work product needs the user's sign-off.

        Read from frontmatter (``user_review:``) every time rather than cached,
        for the same reason :meth:`_critic_for` is: the registry is the single
        source of truth for an agent's declared flow shape, and nothing in the
        engine should hold a second opinion about it.
        """
        try:
            return self._registry.get(name).user_review
        except AgentLoadError:
            return False

    async def _run_unreviewed_author(
        self: EngineHost, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Spawn a sub-agent with no review at all, recording what it produced.

        Reached only when the callee declares neither a ``critic:`` nor
        ``user_review: true`` — one pass, no loop, no gate.

        The ledger is not a review-loop artefact: a role has to be resolvable
        whether or not its producer happens to be reviewed, so an unreviewed
        author's output is recorded exactly like a reviewed one's.

        Its prior work product is still read first, for two reasons that both
        apply even without a review loop: it seeds ``for_revision_paths``, so a
        re-invocation on the same subject continues rather than starting over,
        and it decides the **phase** — an agent asked to redo work it has
        already done is in ``revision``, not ``initial``.

        A sub-agent that reports no paths (a pure-query specialist, an author
        that escalated before writing) simply records nothing; there is no work
        product to record and nothing downstream will ask for one.
        """
        task_input = self._scoped_task_input(name, task_input)
        responsibility = self._responsibility_code(name, task_input)
        round_task = dict(task_input)
        spec = self._registry.spec_for(name)
        # Only an agent that fills an artifact role can have a prior work
        # product, so a pure-query specialist (`investigator`, `web_search`)
        # skips the ledger read entirely rather than looking up an id nothing
        # ever wrote.
        work_product = (
            await self._existing_work_product(name, responsibility)
            if spec is not None and spec.produces
            else None
        )
        revising = work_product is not None and bool(work_product.paths)
        if revising and work_product is not None:
            round_task["for_revision_paths"] = list(work_product.paths)
        resolved, missing = await self._resolve_input_paths(
            name,
            responsibility_code=responsibility,
            caller_paths=task_input.get("input_paths"),
        )
        if missing:
            _log.warning("refusing to spawn %s: unmet required role(s) %s", name, list(missing))
            return self._missing_inputs_result(name, missing)
        if resolved:
            round_task["input_paths"] = resolved
        output = await self._spawn_subagent(
            name, round_task, phase=PHASE_REVISION if revising else PHASE_INITIAL
        )

        paths = _reported_paths(output) or _produced_paths(spec, output)
        if paths and not _escalation_reason(output):
            await self._record_work_product(name, responsibility, paths, output)
        return output

    async def _run_review_loop(
        self: EngineHost,
        author_name: str,
        critic_name: str,
        task_input: dict[str, object],
        max_rounds: int | None,
    ) -> dict[str, object]:
        """Drive review rounds until the work product settles or the budget ends.

        One round is: spawn the author, record the **whole set of files** it
        reported (its work product), then review it. *How* it is reviewed is the
        callee's own declaration, and this method drives both shapes:

        - **A critic** (``critic_name`` non-empty) — hand the set to it, let the
          engine apply its findings to the work product's session-scoped backlog
          (:meth:`_record_findings`, which also fires the user's acceptance gate
          once nothing is outstanding), then read the status back.
        - **The user alone** (``critic_name`` empty, reached only for an author
          declaring ``user_review: true``) — put the set straight to the
          approval gate (:meth:`_run_user_review_round`). A rejection is minted
          as a finding, so the *next* round's author reaches the objection
          through the same ``get_findings`` call it would use for a critic's.

        Either way the status comes from the *stores*, not from a return value:
        the user's own review decision lands there too and can turn an accepted
        work product back into one needing revision.

        Until 2026-09-04 the unit here was a single ``primary_path``: the first
        authors each wrote one document, and the rest of the pipeline inherited
        that shape. A coder does not — a feature spanning five files has to land
        in one go or the build breaks, and reviewing it file-by-file cannot see
        the coherence *between* the files, which is what is most likely to be
        wrong. The reviewable unit is now the whole set (:mod:`kodo.workproducts`).

        The loop stops on any of four things, reported as ``review.outcome``:

        - ``accepted`` — the round left nothing outstanding and the acceptance
          flow settled the work product.
        - ``escalated`` — the author returned a non-empty ``reason``: it hit a
          blocker it cannot defensibly resolve. No review is run and no further
          round is spent — no amount of revision fixes a blocker whose
          resolution lives outside the author.
        - ``max_rounds`` — the budget ran out with findings outstanding.
        - ``not_converging`` — a round closed nothing and opened nothing
          (:attr:`~kodo.findings.RoundSummary.stalled`).

        Every round sends the caller's original ``instructions`` **unchanged**,
        with ``for_revision_paths`` listing the previous round's whole member
        set from round two onward. Outstanding findings are never rendered into
        the task: both halves read them through ``get_findings``.

        What *does* change between rounds is the author's own prompt. A round
        with a prior member set is spawned in :data:`~kodo.subagents.PHASE_REVISION`
        and one without in :data:`~kodo.subagents.PHASE_INITIAL`, so an author
        that declares ``{PHASE:…}`` blocks speaks to the job it is actually
        doing — writing from its inputs, or resolving a backlog against files it
        already wrote. Note this is seeded from the ledger *before* round 1, so
        a re-invocation's first round is correctly a revision.

        Args:
            author_name: The producing sub-agent.
            critic_name: Its critic, or ``""`` when the only reviewer is the
                user at the approval gate.
            task_input: The caller's structured task.
            max_rounds: Round budget, or ``None`` for the default.

        Returns:
            dict: The last round's author output, plus the ``review`` block
                declared by ``run_subagent_<author>``'s output schema.
        """
        budget = max_rounds if isinstance(max_rounds, int) and max_rounds > 0 else None
        budget = min(budget or _DEFAULT_MAX_REVIEW_ROUNDS, _MAX_REVIEW_ROUNDS)
        task_input = self._scoped_task_input(author_name, task_input)
        responsibility = self._responsibility_code(author_name, task_input)

        author_output: dict[str, object] = {}
        summary = RoundSummary(outstanding=0, opened=0, closed=0)
        status = "pending_review"
        outcome = "not_reviewed"
        rounds = 0
        # Seeded from the ledger, not left empty: the Guide routinely invokes
        # the same author again on the same subject ("continue resolving
        # outstanding findings"), and this call's round 1 is that work's round
        # N. Without this the author would be told to revise nothing on the
        # first round of every re-invocation, and its findings would be scoped
        # to no work product — the two things it needs to carry on at all.
        work_product = await self._existing_work_product(author_name, responsibility)

        for _round in range(budget):
            rounds += 1
            round_task = dict(task_input)
            revising = work_product is not None and bool(work_product.paths)
            if revising and work_product is not None:
                round_task["for_revision_paths"] = list(work_product.paths)
            resolved, missing = await self._resolve_input_paths(
                author_name,
                responsibility_code=responsibility,
                caller_paths=task_input.get("input_paths"),
            )
            if missing:
                _log.warning(
                    "refusing to spawn %s: unmet required role(s) %s", author_name, list(missing)
                )
                author_output = self._missing_inputs_result(author_name, missing)
                outcome = "escalated"
                break
            if resolved:
                round_task["input_paths"] = resolved
            author_output = await self._spawn_subagent(
                author_name,
                round_task,
                work_product.id if work_product else "",
                phase=PHASE_REVISION if revising else PHASE_INITIAL,
            )

            # An escalation ends the loop where it stands: the author is telling
            # its caller the blocker is not one more revision away, so sending it
            # to the critic would only spend a round producing findings nobody
            # can act on. The result carries reason/summary/options straight back.
            if _escalation_reason(author_output):
                outcome = "escalated"
                break

            paths = _reported_paths(author_output)
            if not paths:
                _log.warning("run_subagent: %s reported no paths", author_name)
                outcome = "not_reviewed"
                break

            work_product = await self._record_work_product(
                author_name, responsibility, paths, author_output
            )
            if work_product is None:
                outcome = "not_reviewed"
                break

            if critic_name:
                status, summary = await self._run_review_round(
                    critic_name, work_product, rounds, budget
                )
            else:
                status, summary = await self._run_user_review_round(work_product, rounds, budget)

            if status in ("accepted", "pending_acceptance"):
                outcome = "accepted"
                break
            # A round that closed nothing and opened nothing is stalled: another
            # pass is unlikely to converge, and the caller can act on the
            # outstanding backlog now rather than in five rounds.
            if summary.stalled:
                outcome = "not_converging"
                break
            outcome = "max_rounds"

        _log.info(
            "review loop finished: author=%s critic=%s work_product=%s rounds=%d outcome=%s "
            "status=%s outstanding=%d",
            author_name,
            critic_name or "(user gate)",
            work_product.id if work_product else "-",
            rounds,
            outcome,
            status,
            summary.outstanding,
        )
        return {
            **author_output,
            "review": {
                "status": status,
                "outcome": outcome,
                "rounds": rounds,
                "outstanding": summary.outstanding,
            },
        }

    async def _existing_work_product(
        self: EngineHost, author_name: str, responsibility_code: str
    ) -> WorkProduct | None:
        """This author's prior work product for *responsibility_code*, if any.

        Identity is derived, and the ledger is project-scoped, so a work product
        outlives both the call that created it and the session — which is what
        makes "carry on from where the last round left off" answerable at all.
        """
        roots = self._root_paths()
        if not roots:
            return None
        project = roots[0].name
        project_root = self._project_root(project)
        if project_root is None:
            return None
        wp_id = work_product_id(project, author_name, responsibility_code)
        return await asyncio.to_thread(read_work_product, project_root, wp_id)

    async def _record_work_product(
        self: EngineHost,
        author_name: str,
        responsibility_code: str,
        paths: list[str],
        output: dict[str, object] | None = None,
    ) -> WorkProduct | None:
        """Persist this round's member set and auto-close findings for files that left.

        The project is taken from the first member path's folder prefix — every
        path an agent reports is folder-prefixed with its owning root's name, so
        the set carries its own project. A set spanning two projects is a model
        error rather than a supported shape; the first path wins and the rest
        still ride along as members, since refusing the round outright would
        lose work already written to disk.

        *output* is the author's whole ``return_result`` payload, from which the
        role map is derived (:meth:`_role_map`) — that is what later stages
        resolve their declared needs against.

        A file that leaves the membership takes its findings with it: nothing
        will ever re-read it, so an outstanding finding against it could never
        be verified fixed and would block the loop forever
        (:func:`~kodo.findings.close_findings_for_paths`).
        """
        project = paths[0].split("/", 1)[0]
        project_root = self._project_root(project)
        if project_root is None:
            _log.warning(
                "%s reported paths under unbound root %r; work product not recorded",
                author_name,
                project,
            )
            return None
        try:
            work_product, removed = await asyncio.to_thread(
                record_membership,
                project_root,
                project=project,
                agent=author_name,
                responsibility_code=responsibility_code,
                paths=paths,
                roles=self._role_map(author_name, paths, output or {}),
                components=self._component_map(author_name, paths, output or {}),
            )
        except ValueError as exc:
            _log.warning("work product not recorded for %s: %s", author_name, exc)
            return None

        if removed:
            findings_dir = self._findings_dir()
            if findings_dir is not None:
                closed = await asyncio.to_thread(
                    close_findings_for_paths, findings_dir, work_product.id, removed
                )
                _log.info(
                    "work product %s dropped %s; auto-closed %d finding(s): %s",
                    work_product.id,
                    list(removed),
                    len(closed),
                    closed,
                )
        await self._record_components(author_name, project, output or {})
        return work_product

    @staticmethod
    def _missing_inputs_result(agent_name: str, missing: tuple[str, ...]) -> dict[str, object]:
        """The escalation returned instead of spawning an under-supplied agent.

        Shaped exactly like an author's own escalation (a non-empty ``reason``),
        so the calling agent reads it through the path it already has for "this
        could not be done and no revision fixes it" — and the review loop stops
        on it without spending a round.

        Refusing is the whole point. An agent whose contract promises it the
        architecture, spawned without one, does not fail cleanly: it invents a
        plausible path and reads it until something stops it. The named role in
        this message is what a caller needs to fix the ordering — almost always
        an upstream stage that has not run yet.
        """
        listed = ", ".join(missing)
        return {
            "summary": (
                f"{agent_name} was not spawned: its contract requires "
                f"{listed}, and nothing in this project has produced "
                f"{'them' if len(missing) > 1 else 'it'} yet."
            ),
            "reason": "missing_required_input",
            "options": [
                f"Run the stage that produces {listed}, then retry this one.",
                "If that stage was skipped deliberately, the pipeline order needs revisiting.",
            ],
        }

    def _project_root(self: EngineHost, project: str) -> Path | None:
        """The bound root directory named *project*, or ``None``.

        The work-product ledger is project-scoped (it records what a project
        *contains*, which outlives any one session), so every read and write of
        it needs the root, not just the logical folder name every agent path
        carries.
        """
        for root in self._root_paths():
            if root.name == project:
                return Path(root.path)
        return None

    def _component_map(
        self: EngineHost, agent_name: str, paths: list[str], output: dict[str, object]
    ) -> dict[str, str]:
        """``{path: component codename}`` for an agent that writes several in one run.

        Only ``functional_designer`` declares a ``component_paths`` field today:
        it writes every component's Functional Design in a single whole-product
        call, so its work product carries no ``responsibility_code`` and this is
        the only thing that lets ``self``/``dependencies`` narrow to one design
        rather than matching all of them or none.
        """
        spec = self._registry.spec_for(agent_name)
        field_name = getattr(spec, "component_paths", "") if spec is not None else ""
        if not field_name:
            return {}
        declared = output.get(field_name)
        if not isinstance(declared, dict):
            return {}
        members = set(paths)
        return {
            value: str(code)
            for code, value in declared.items()
            if isinstance(value, str) and value in members
        }

    async def _record_components(
        self: EngineHost, agent_name: str, project: str, output: dict[str, object]
    ) -> None:
        """Persist the architect's component graph, when it returned one.

        The decomposition already exists in the architecture document as prose;
        declaring it as data is what lets a later stage be handed a component's
        neighbours automatically instead of every reader re-deriving the same
        graph. Only the agent producing the architecture is consulted, so no
        other output shape can accidentally overwrite it.
        """
        spec = self._registry.spec_for(agent_name)
        if spec is None or ROLE_ARCHITECTURE not in spec.produces:
            return
        raw = output.get("components")
        if not isinstance(raw, list):
            return
        graph: dict[str, list[str]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).strip()
            if not code:
                continue
            deps = entry.get("depends_on")
            graph[code] = (
                [str(d) for d in deps if isinstance(d, str)] if isinstance(deps, list) else []
            )
        project_root = self._project_root(project)
        if not graph or project_root is None:
            return
        await asyncio.to_thread(record_components, project_root, project=project, components=graph)
        _log.info("recorded component graph for %s: %s", project, sorted(graph))

    def _role_map(
        self: EngineHost, agent_name: str, paths: list[str], output: dict[str, object]
    ) -> dict[str, list[str]]:
        """Which of *paths* fills each artifact role this agent declares.

        Read off the producing agent's ``produces`` map (doc/FINDINGS.md), never
        off anything the model labelled: the model reports *where* it wrote,
        the spec says *what* those files are for.

        A role mapped to a named output field takes the path that field holds;
        the role mapped to :data:`~kodo.subagents.PRODUCES_REMAINDER` takes
        everything no named field claimed. That remainder rule is what keeps
        ``functional_designer``'s Design Plan out of the pile of Functional
        Designs it wrote in the same run, without either of them having to be
        enumerated.
        """
        spec = self._registry.spec_for(agent_name)
        if spec is None or not spec.produces:
            return {}

        claimed: set[str] = set()
        roles: dict[str, list[str]] = {}
        remainder_role = ""
        for role, field_name in spec.produces.items():
            if field_name == PRODUCES_REMAINDER:
                remainder_role = role
                continue
            value = output.get(field_name)
            named = [value] if isinstance(value, str) and value.strip() else []
            if named:
                roles[role] = named
                claimed.update(named)
        if remainder_role:
            rest = [p for p in paths if p not in claimed]
            if rest:
                roles[remainder_role] = rest
        return roles

    async def _run_review_round(
        self: EngineHost,
        critic_name: str,
        work_product: WorkProduct,
        iteration: int = 1,
        max_rounds: int = _DEFAULT_MAX_REVIEW_ROUNDS,
    ) -> tuple[str, RoundSummary]:
        """Spawn *critic_name* against *work_product*; return its ``(status, summary)``.

        The status is read back from the stores rather than from the critic's
        own return value: :meth:`_record_findings` has already applied the
        round's findings, and once nothing is outstanding it also ran the user's
        acceptance gate (when the author declares one), whose decision is the
        later event and therefore the real current state.

        Args:
            critic_name: The critic to run this round.
            work_product: The set under review. Every member reaches the critic
                through its ``under_review`` need — the whole point of the unit
                is that cross-file defects are only visible with all of it in
                hand — alongside whatever else its ``consumes`` declares.
            iteration: 1-based round number, for the user's findings table.
            max_rounds: The loop's budget, so that table reads "2 of 5".
        """
        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_STARTED,
                {
                    "reviewer_name": critic_name,
                    "target_filename": work_product.paths[0] if work_product.paths else "",
                    "target_filenames": list(work_product.paths),
                    "work_product_id": work_product.id,
                    "target_type": "document",
                },
            )
        )
        before = await self._findings_snapshot(work_product.id)
        critic_paths, missing = await self._resolve_input_paths(
            critic_name,
            responsibility_code=work_product.responsibility_code,
            under_review=work_product,
        )
        if missing:
            # A critic's inputs are built entirely by the engine, so a miss here
            # is unambiguous. Reviewing on less than its contract promises is
            # how five of six critic runs in the traced session silently
            # produced degraded reviews nobody flagged.
            _log.warning(
                "skipping review round: %s requires unmet role(s) %s",
                critic_name,
                list(missing),
            )
            return await self._work_product_status(work_product), RoundSummary(
                outstanding=0, opened=0, closed=0
            )
        await self._spawn_subagent(
            critic_name,
            {
                "instructions": _review_instructions(work_product.paths),
                "input_paths": critic_paths,
            },
            work_product.id,
        )
        after = await self._findings_snapshot(work_product.id)
        summary = RoundSummary(
            outstanding=sum(1 for state in after.values() if state == STATE_OUTSTANDING),
            opened=sum(1 for finding_id in after if finding_id not in before),
            closed=sum(
                1
                for finding_id, state in after.items()
                if state != STATE_OUTSTANDING and before.get(finding_id) == STATE_OUTSTANDING
            ),
        )

        status = await self._work_product_status(work_product)

        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_VERDICT,
                {
                    "reviewer_name": critic_name,
                    "target_filename": work_product.paths[0] if work_product.paths else "",
                    "target_filenames": list(work_product.paths),
                    "work_product_id": work_product.id,
                    "verdict": status,
                    "outstanding": summary.outstanding,
                    "opened": summary.opened,
                    "closed": summary.closed,
                },
            )
        )
        await self._emit_review_findings(
            work_product, reviewer=critic_name, iteration=iteration, max_rounds=max_rounds
        )
        return status, summary

    async def _run_user_review_round(
        self: EngineHost, work_product: WorkProduct, iteration: int, max_rounds: int
    ) -> tuple[str, RoundSummary]:
        """Put *work_product* straight to the user's approval gate; report the outcome.

        The critic-free half of :meth:`_run_review_loop`, for an author that
        declares ``user_review: true`` and no ``critic:``. The user *is* the
        reviewer, so this round is the gate itself: approval settles the work
        product, a rejection is minted as a ``user_feedback`` finding
        (:meth:`~._core.EngineCore._finalize_work_product`) and the loop spends
        another round on it — the author reading that objection through the same
        ``get_findings`` call it would use for a critic's finding.

        The ``(status, summary)`` contract matches :meth:`_run_review_round`
        exactly, so the loop above needs no special case: the counters come from
        the backlog either side of the gate, which keeps the ``not_converging``
        stall guard working here too.

        Note the gate can decline to fire at all — autonomous mode and Edit
        Control *Allow All* both accept without asking — in which case this
        returns ``accepted`` on the first round, which is the correct answer.
        """
        before = await self._findings_snapshot(work_product.id)
        await self._finalize_work_product(work_product)
        after = await self._findings_snapshot(work_product.id)
        summary = RoundSummary(
            outstanding=sum(1 for state in after.values() if state == STATE_OUTSTANDING),
            opened=sum(1 for finding_id in after if finding_id not in before),
            closed=sum(
                1
                for finding_id, state in after.items()
                if state != STATE_OUTSTANDING and before.get(finding_id) == STATE_OUTSTANDING
            ),
        )
        status = await self._work_product_status(work_product)
        _log.info(
            "user review round: work_product=%s round=%d/%d status=%s outstanding=%d",
            work_product.id,
            iteration,
            max_rounds,
            status,
            summary.outstanding,
        )
        await self._emit_review_findings(
            work_product,
            reviewer=USER_FEEDBACK_REPORTER,
            iteration=iteration,
            max_rounds=max_rounds,
        )
        return status, summary

    async def _emit_review_findings(
        self: EngineHost,
        work_product: WorkProduct,
        *,
        reviewer: str,
        iteration: int,
        max_rounds: int,
    ) -> None:
        """Push the user's findings table for the round that just finished.

        Emitted after any round that could have changed the backlog, which is
        every critic round and every trip through the approval gate. It is the
        only thing in the protocol that tells the *user* what is actually wrong
        with a work product — ``review.verdict`` carries counts, and the
        findings themselves otherwise never leave the author/critic pair.

        Silent when the backlog is empty. A work product that was right first
        time has nothing to table, and emitting an empty one on every clean
        accept would train the reader to skip it.

        Called after the critic's subsession has closed, so
        ``EngineEmitters._append_marker`` writes to the **main** session log
        rather than the subsession's — the table belongs in the feed next to the
        collapsed review block, not buried inside it.
        """
        findings_dir = self._findings_dir()
        if findings_dir is None:
            return
        findings = await asyncio.to_thread(read_findings, findings_dir, work_product.id)
        if not findings:
            return
        await self._emitters.emit_review_findings(
            work_product_id=work_product.id,
            agent=work_product.agent,
            reviewer_name=reviewer,
            iteration=iteration,
            max_rounds=max_rounds,
            paths=list(work_product.paths),
            findings=[dict(finding) for finding in sort_for_display(findings)],
        )

    async def _resolve_input_paths(
        self: EngineHost,
        agent_name: str,
        *,
        responsibility_code: str = "",
        under_review: WorkProduct | None = None,
        caller_paths: object = None,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the ``input_paths`` a spawn receives, from what the project holds.

        The agent's ``consumes`` declaration says which artifact *roles* it
        needs; the ledger says which files currently fill them. Neither half
        involves a model, which is the point: until 2026-09-05 an author's input
        paths were whatever the calling LLM typed and a critic's were a
        hardcoded ``{"target": path}``, and a critic promised the architecture
        but handed only the document under review invented the architecture's
        path and read the resulting nonexistent file ~1133 times.

        Labelling is ``<role>`` for a single file and ``<role>_<basename>`` for
        several, so the rendered task says what each path *is* rather than
        numbering them.

        *caller_paths* is merged underneath: anything the caller named that
        resolution did not produce survives, and a resolved role always wins the
        same label. For a pipeline agent this is belt-and-braces — the field is
        stripped from its tool, so a caller has nothing to supply. It is load
        bearing for an agent that declares no roles at all (``developer``, whose
        tool therefore keeps ``input_paths``): resolution builds nothing, and
        what its caller named is the whole answer. A critic never has one.

        Returns:
            tuple: the resolved ``input_paths``, and the roles of any
                **required** need nothing filled. The caller refuses the spawn
                on a non-empty second element (:meth:`_missing_inputs_result`) —
                an agent handed less than its contract promises is an agent that
                will invent the difference.
        """
        spec = self._registry.spec_for(agent_name)
        # Accumulated per role before labelling, because one role legitimately
        # appears at more than one scope: `coder` asks for its own component's
        # Functional Design *and* its neighbours'. Labelling each need on its
        # own would give both the bare role name and silently drop the first.
        by_role: dict[str, list[str]] = {}
        unmet: list[str] = []
        if spec is not None and spec.consumes:
            project = self._resolution_project(under_review)
            project_root = self._project_root(project)
            work_products = (
                await asyncio.to_thread(read_work_products, project_root)
                if project_root is not None
                else []
            )
            components = (
                await asyncio.to_thread(read_components, project_root, project)
                if project_root is not None
                else {}
            )
            needs = [(n.role, n.scope, n.required) for n in spec.consumes]
            for need in resolve_needs(
                work_products,
                needs,
                project=project,
                responsibility_code=responsibility_code,
                under_review=under_review,
                components=components,
            ):
                if need.unmet:
                    unmet.append(need.role)
                collected = by_role.setdefault(need.role, [])
                for path in need.paths:
                    if path not in collected:
                        collected.append(path)

        resolved: dict[str, str] = {}
        for role, paths in by_role.items():
            resolved.update(_role_labels(role, tuple(paths)))

        inherited: dict[str, str] = {}
        if isinstance(caller_paths, dict):
            inherited = {
                str(label): value
                for label, value in caller_paths.items()
                if isinstance(value, str) and value.strip()
            }
        return {**inherited, **resolved}, tuple(dict.fromkeys(unmet))

    def _resolution_project(self: EngineHost, under_review: WorkProduct | None) -> str:
        """Which bound root's artifacts this spawn should resolve against.

        A review round names it outright — the work product under review. Outside
        one, the session's first bound root: a session with a single project (the
        overwhelmingly common case) has only one answer, and a multi-project
        session's Guide drives one project at a time through paths that already
        carry their own prefix.
        """
        if under_review is not None:
            return under_review.project
        roots = self._root_paths()
        return roots[0].name if roots else ""

    def _findings_dir(self: EngineHost) -> Path | None:
        """This session's ``findings/`` directory, or ``None`` before one is attached.

        The single place the session-scoped findings root is derived
        (doc/FINDINGS.md §2). Note it cannot be derived from a tool's
        ``ToolContext.session_id``: inside a sub-agent run that field holds the
        *subsession* id, which is why the engine injects the directory instead.
        """
        try:
            return self._transient.session_dir / "findings"
        except (AssertionError, AttributeError):
            # No session attached yet (``session_dir`` asserts), or a bare test
            # host with no store at all. Either way there is no backlog to read,
            # and every caller already treats ``None`` as "empty".
            return None

    async def _findings_snapshot(self: EngineHost, key: str) -> dict[str, str]:
        """``{finding id: state}`` for work product *key* right now, or ``{}``.

        The round's ``opened``/``closed`` deltas are computed by diffing this
        before and after the critic's subsession rather than by threading a
        return value out of :meth:`_record_findings` — which runs several frames
        down inside :meth:`_drive_subsession`, and does not run at all for a
        completed subsession replayed from the ledger.
        """
        findings_dir = self._findings_dir()
        if findings_dir is None:
            return {}
        findings = await asyncio.to_thread(read_findings, findings_dir, key)
        return {f["id"]: f["state"] for f in findings}

    async def _work_product_status(self: EngineHost, work_product: WorkProduct) -> str:
        """The status of a whole work product: every member file must be settled.

        Each member's own project evolution log is merged with the work
        product's shared findings backlog (:func:`kodo.tools.document_status`,
        doc/FINDINGS.md §6) and the **weakest** answer wins — a set is only
        accepted when all of it is. That conjunction is the point of the unit:
        a five-file change with one file still pending review is not a change
        anyone should be told is done.

        An unresolvable or unbound member reads as ``pending_review`` — "not
        settled" — which is what the loop treats it as anyway.
        """
        if not work_product.paths:
            return "pending_review"
        statuses = [await self._member_status(path, work_product.id) for path in work_product.paths]
        # Weakest-wins, in ascending order of "settled".
        for weakest in ("needs_revision", "pending_review", "pending_acceptance"):
            if weakest in statuses:
                return weakest
        return "accepted"

    async def _member_status(self: EngineHost, path: str, key: str) -> str:
        """One member file's status: its own evolution log + the shared backlog."""
        try:
            resolved = self._make_resolver(self._orch_session_id).resolve(path)
        except PermissionError:
            _log.warning("review round: %r cannot be resolved", path)
            return "pending_review"
        owning_root = root_for(self._root_paths(), resolved)
        if owning_root is None:
            _log.warning("review round: %r is not under any bound root", path)
            return "pending_review"
        return await asyncio.to_thread(
            document_status, resolved, Path(owning_root.path), self._findings_dir(), key
        )

    async def _record_findings(
        self: EngineHost, reviewer: str, output: dict[str, object], key: str
    ) -> None:
        """Apply a finished critic's findings to its work product's backlog.

        The engine-side half of a critic round: create the new findings, patch
        the ones it updated, close the round with a ``review_round`` entry — then,
        when nothing is left outstanding, drive the acceptance flow
        (:meth:`~._core.EngineCore._finalize_work_product`), which auto-accepts
        in autonomous mode or under Edit Control *Allow All* and otherwise asks
        the user to sign off.

        There is no ``accept`` field to consult: the verdict is *derived* from
        the resulting backlog, so a critic cannot report a pass while leaving
        problems open (doc/FINDINGS.md §3).

        The subject comes from *key* — the work product this subsession was
        spawned against — not from the critic's own output. Until 2026-09-04 it
        was read from a ``path`` the critic returned, which meant a critic could
        misreport what it had reviewed and write findings into the wrong
        backlog. The engine already knows; asking the model was never necessary.

        Called from :meth:`_drive_subsession` for every agent whose frontmatter
        declares ``role: critic``, so it applies to a critic reached through a
        review loop *and* to one resumed mid-flight after a crash, but never to
        a completed subsession being replayed from the ledger.

        A malformed verdict is logged and dropped rather than raised: the loop
        reads the stores for the real status, and a critic that failed to report
        leaves the backlog untouched, which the loop reads as a stalled round.
        """
        if not key:
            _log.warning("critic %s ran outside a work product; nothing recorded", reviewer)
            return
        raw = output.get("findings")
        updates = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
        findings_dir = self._findings_dir()
        # The work product's id is `<project>/<agent>[/<responsibility>]`, so its
        # own key names the root its membership log lives under.
        project_root = self._project_root(key.split("/", 1)[0])
        if findings_dir is None or project_root is None:
            _log.warning("critic %s reported on %r with no store to read", reviewer, key)
            return
        work_product = await asyncio.to_thread(read_work_product, project_root, key)
        if work_product is None:
            _log.warning("critic %s reported on unknown work product %r", reviewer, key)
            return
        try:
            summary = await asyncio.to_thread(
                apply_findings,
                findings_dir,
                key,
                reviewer=reviewer,
                updates=updates,
                project=work_product.project,
                agent=work_product.agent,
                responsibility_code=work_product.responsibility_code,
            )
        except ValueError as exc:
            _log.info("critic %s findings on %r not recorded: %s", reviewer, key, exc)
            return
        _log.info(
            "critic %s reviewed %s (%d file(s)): outstanding=%d opened=%d closed=%d",
            reviewer,
            key,
            len(work_product.paths),
            summary.outstanding,
            summary.opened,
            summary.closed,
        )
        if summary.outstanding == 0:
            await self._finalize_work_product(work_product)

    async def _run_dependency_manager(
        self: EngineHost, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Spawn the dependency-management sub-agent for the ``toolchain_deps`` tool.

        Ungated by design: the tool's possession is the authorization, so the
        fixed ``toolchain_depsmgr`` agent is driven straight through
        :meth:`_spawn_subagent` without an allow-list check and without sitting
        in any caller's ``subagents:`` allow-list — keeping every dependency change on
        the single ``toolchain_deps`` path (which alone knows how to translate a
        missing ``DEPENDENCIES.md`` into a remediation message).

        Args:
            task_input: Structured task conforming to ``toolchain_depsmgr``'s
                ``input_schema``.

        Returns:
            dict: The sub-agent's ``output_schema`` result.
        """
        return await self._spawn_subagent(_DEPSMGR_AGENT_NAME, task_input)

    async def _run_web_search_agent(
        self: EngineHost, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        """Run the ``web_search`` agent for the ``web_search`` tool (doc/WEB_SEARCH.md).

        Ungated by design (holding ``web_search`` is the authorization,
        mirroring :meth:`_run_dependency_manager`) — but unlike the depsmgr it
        is *not* a subsession: ``web_search`` is typically called from the
        investigator, itself a sub-agent, and subsessions do not nest.
        Instead the agent drives its own multi-round research loop via
        :meth:`_run_silent_tool_loop_turn`: no feed events or subsession
        markers, only its USD cost folded into the session total.

        ``task_input["timeout"]`` (already clamped to
        :data:`_MAX_WEB_SEARCH_TIMEOUT_S` by the tool) bounds the run; it is
        re-clamped here too so this method stays safe for any other caller.
        On a timeout with no usable result, a fallback ``{themes: [], note}``
        is synthesized rather than raising — ``web_search`` never errors the
        calling agent's turn.

        Every round in which the agent produces free text is streamed live to
        the client as ``web_search.note`` (``tool_call_id`` correlates it with
        the ``web_search`` call's own tool-call card) and buffered; once the
        run ends the full buffer is written to a best-effort sidecar file
        (:meth:`~kodo.state.TransientStore.write_web_search_notes`) so
        ``session.history`` can replay it into the "Web Search" block on
        reload. Nothing here touches ``session.jsonl``/the subsession log, so
        a crash mid-run just loses whatever wasn't written yet — acceptable,
        since this narration is a visibility aid, not part of the agent's own
        conversation (doc/WEB_SEARCH.md §6).

        Args:
            task_input: ``{query, max_themes, timeout}`` per the sub-agent's
                ``input_schema``.
            tool_call_id: The ``web_search`` tool_use block id (the calling
                agent's ``ToolContext.current_tool_use_id``), correlating the
                live notes and their sidecar file with that call's card.

        Returns:
            dict: ``{"themes": [...], "note": "..."}``.
        """
        agent = self._registry.get(_WEB_SEARCH_AGENT_NAME)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)

        timeout_raw = task_input.get("timeout")
        timeout = (
            min(float(timeout_raw), _MAX_WEB_SEARCH_TIMEOUT_S)
            if isinstance(timeout_raw, (int, float)) and timeout_raw > 0
            else _DEFAULT_WEB_SEARCH_TIMEOUT_S
        )
        deadline = time.time() + timeout

        session_id = f"web-search-{uuid.uuid4().hex}"
        dispatcher = self._make_dispatcher(_WEB_SEARCH_AGENT_NAME, session_id, deadline=deadline)
        web_search_spec = self._registry.spec_for(_WEB_SEARCH_AGENT_NAME)
        messages: list[Message] = [
            Message(
                role="user",
                content=self._render_task_input(
                    task_input, web_search_spec.input_schema if web_search_spec else None
                ),
            )
        ]

        notes: list[str] = []

        async def _on_round_text(text: str) -> None:
            notes.append(text)
            await self._emitters.emit_web_search_note(tool_call_id, text)

        try:
            result = await self._run_silent_tool_loop_turn(
                routing,
                plugin,
                model_id,
                agent,
                messages,
                dispatcher,
                deadline,
                on_round_text=_on_round_text,
            )
        finally:
            if notes:
                self._transient.write_web_search_notes(tool_call_id, notes)

        if result is not None:
            themes = result.get("themes")
            note = result.get("note")
            return {
                "themes": themes if isinstance(themes, list) else [],
                "note": note if isinstance(note, str) else "",
            }
        _log.info("web_search agent produced no result within its time budget")
        return {"themes": [], "note": "Search timed out before a report could be produced."}

    @staticmethod
    def _render_param_value(value: object, indent: str = "") -> str:
        """Pretty-print one Input Parameters value as markdown, recursing into containers.

        A scalar renders inline. A flat list of scalars renders comma-joined
        (unchanged from before). A dict, or a list containing one, renders as a
        nested bullet list instead of a Python repr — task inputs regularly
        carry a labeled path collection (``input_paths: {"architecture": "..."}``),
        and a smaller local model reads that far more reliably as markdown
        bullets than as ``{'architecture': '...'}``. ``None`` (an omitted optional
        field, e.g. ``for_revision_paths`` on a first round) renders as
        ``(none)`` rather than the Python literal ``None``.
        """
        if value is None:
            return "(none)"
        if isinstance(value, dict):
            if not value:
                return "(none)"
            return "\n" + "\n".join(
                f"{indent}  - **{k}**: {SubagentMixin._render_param_value(v, indent + '  ')}"
                for k, v in value.items()
            )
        if isinstance(value, list):
            if not value:
                return "(none)"
            if all(not isinstance(v, (dict, list)) for v in value):
                return ", ".join(str(v) for v in value)
            return "\n" + "\n".join(
                f"{indent}  - {SubagentMixin._render_param_value(v, indent + '  ')}" for v in value
            )
        return str(value)

    @staticmethod
    def _render_task_input(
        task_input: dict[str, object], input_schema: dict[str, object] | None = None
    ) -> str:
        """Render a structured ``task_input`` to the user turn the sub-agent reads.

        ``instructions`` becomes the ``# Task`` heading. Every other field the
        sub-agent was actually given is pretty-printed under a trailing
        ``## Input Parameters`` section — the last section of this message, and
        (since a local model's chat template concatenates system prompt and
        first user turn into one flat string) the last section of the whole
        prompt the agent's own ``.md`` file promises it. This is also the only
        place the sub-agent still sees a description of `return_result`'s job:
        the registry no longer restates the input schema in the system prompt
        (see ``AgentRegistry``'s dropped ``## Your Task Contract``), so this
        per-call section is what replaces it, populated with real values
        instead of a schema.

        Args:
            task_input: The concrete task this call is spawning the sub-agent
                with.
            input_schema: The sub-agent's declared ``input_schema`` (from its
                ``SubAgentSpec``), used only to (a) order fields the way the
                spec declares them and (b) pull each field's ``description`` so
                a smaller local model doesn't have to infer what a bare value
                means. ``None`` for a sub-agent with no spec (should not happen
                for a real spawn, but degrades gracefully — no descriptions, no
                `return_result` reminder, caller-supplied field order).

        This is what the LLM sees; the UI renders the same task as a distinct
        *task brief* entry (see the ``subagent_task`` entry kind), not as a
        user prompt bubble.
        """
        if not task_input:
            return "(no task)"
        lines: list[str] = []
        instructions = task_input.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            lines.append("# Task\n\n" + instructions.strip())
        others = {k: v for k, v in task_input.items() if k != "instructions"}
        properties_raw = input_schema.get("properties") if isinstance(input_schema, dict) else None
        properties: dict[str, object] = properties_raw if isinstance(properties_raw, dict) else {}
        ordered_keys = [k for k in properties if k in others]
        ordered_keys += [k for k in others if k not in ordered_keys]

        if input_schema is not None:
            param_lines = ["## Input Parameters"]
            if ordered_keys:
                for key in ordered_keys:
                    prop = properties.get(key)
                    description = prop.get("description") if isinstance(prop, dict) else None
                    label = f"**{key}**"
                    if isinstance(description, str) and description.strip():
                        label += f" ({description.strip()})"
                    rendered = SubagentMixin._render_param_value(others[key])
                    param_lines.append(f"- {label}: {rendered}")
            else:
                param_lines.append("(no parameters beyond the task above)")
            param_lines.append("")
            param_lines.append(_RETURN_RESULT_REMINDER)
            lines.append("\n".join(param_lines))
        elif others:
            # Defensive fallback for a sub-agent spawned with no spec on record
            # (should not happen in practice — every real spawn target is
            # schema-bearing). No descriptions, no return_result reminder.
            param_lines = ["## Input Parameters"]
            for key in ordered_keys:
                param_lines.append(f"- **{key}**: {SubagentMixin._render_param_value(others[key])}")
            lines.append("\n".join(param_lines))
        return "\n\n".join(lines) or "(no task)"

    async def _spawn_subagent(
        self: EngineHost,
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = PHASE_INITIAL,
    ) -> dict[str, object]:
        """Invoke a leaf sub-agent and return its structured result.

        The ungated spawn primitive: callers that have already passed the
        permission gate (:meth:`_run_subagent`, or
        :meth:`_run_review_loop`, whose names were gated up front)
        drive a subsession through here.

        Args:
            name: Sub-agent name from the registry.
            task_input: Structured task conforming to the sub-agent's input schema.
            findings_key: The document this run's review round targets, which
                binds ``get_findings``' auto-scope for the whole subsession
                (doc/FINDINGS.md §3). Empty for any spawn that is not part of a
                review round, and for an author's first pass — the tool then
                answers with an empty list rather than an error.
            phase: Which ``{PHASE:…}`` blocks the agent's prompt keeps —
                ``initial`` when it is producing from its inputs, ``revision``
                when it is resolving findings against files it already wrote.
                Defaults to ``initial``, the fuller text, so an engine-driven
                spawn that has no notion of rounds (``compactor``,
                ``web_search``, the dependency manager) is never silently
                under-instructed.

        Returns:
            dict: The structured result the sub-agent returned via ``return_result``.
        """
        if name in _DIRECT_ONLY_AGENTS:
            _log.warning("spawn_subagent: %r is engine-driven only and cannot be invoked", name)
            return {}

        # During a crash-resume replay, each run_subagent call consumes the next
        # subsession marker recorded before the crash instead of starting fresh.
        # An exhausted/empty ledger means no marker was recorded for this call
        # (crash landed before the subsession opened) — fall through to a fresh run.
        if self._replay_subsessions:
            return await self._replay_next_subsession(name, findings_key, phase)
        self._replay_subsessions = None

        subsession_id = uuid.uuid4().hex
        spec = self._registry.spec_for(name)
        seed_content = self._render_task_input(task_input, spec.input_schema if spec else None)
        await self._open_subsession(name, subsession_id, seed_content)

        seed = Message(role="user", content=seed_content)
        # Persisted/displayed as a distinct task brief, not a user prompt bubble.
        self._transient.append_subsession_message(
            subsession_id, seed.role, seed.content, kind="subagent_task"
        )

        output = await self._drive_subsession(name, subsession_id, [seed], findings_key, phase)
        await self._close_subsession(name, subsession_id, output)
        return output

    async def _drive_subsession(
        self: EngineHost,
        name: str,
        subsession_id: str,
        messages: list[Message],
        findings_key: str = "",
        phase: str = PHASE_INITIAL,
    ) -> dict[str, object]:
        """Run a sub-agent's isolated turn loop and return its structured result.

        Used for both a fresh subsession and a resumed one (``messages`` already
        rehydrated from the subsession log). Sub-agent messages persist into the
        subsession file at every turn boundary so the run is resumable mid-flight.
        The structured result is whatever the agent passed to ``return_result``
        (validated against its output schema); if it never called it, a bare
        ``{schema_compliance: False}`` fallback is synthesized — there is no
        artifact index to recover a partial result from, so the caller (e.g.
        ``_run_review_loop``) just sees an empty result and treats
        it as if nothing happened.
        """
        agent = self._registry.get(name, self._session.effective_autonomous, phase)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)
        dispatcher = self._make_dispatcher(name, subsession_id, findings_key=findings_key)
        leaf_tools = agent_tool_specs(self._registry, agent)

        self._session.phase = "running"
        self._session.agent = name
        await self._emitters.emit_state()

        stream_id = uuid.uuid4().hex
        await self._emitters.emit_agent_started(name)

        def _persist(batch: list[Message]) -> None:
            for msg in batch:
                self._transient.append_subsession_message(subsession_id, msg.role, msg.content)

        await self._run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=messages,
            tools=leaf_tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=name,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=_persist,
            subsession_model_key=model_id,
            on_stall=self._make_stall_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
                dispatcher=dispatcher,
            ),
            on_cyclic_thinking=self._make_cyclic_thinking_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_think_in_tool_call=self._make_think_in_tool_call_handler(
                agent_name=name,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_tool_call_cyclic=self._make_tool_call_cyclic_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_repeated_tool_calls=self._make_repeated_tool_call_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
        )

        # Safety net for a final round with zero deltas — see the matching
        # comment in ``_turns.py``'s entry-turn caller.
        self._session.awaiting_first_chunk = False
        await self._sink.send(Envelope.make_stream_end(stream_id))
        await self._emitters.emit_agent_finished(name)
        output = dispatcher.returned_output
        if output is None:
            _log.warning(
                "subsession %s (%s) ended without return_result; synthesizing fallback",
                subsession_id,
                name,
            )
            output = {SCHEMA_COMPLIANCE_KEY: False}
        _log.info(
            "subsession completed: name=%s id=%s keys=%s",
            name,
            subsession_id,
            sorted(output.keys()),
        )
        # A critic's result is not a value handed back to whoever spawned it —
        # it is a set of findings against a file, and the session's findings
        # backlog is where they belong. Recording them here (rather than in the
        # critic's own toolset, as the retired ``document_feedback`` did) keeps
        # every spawn path covered: a review loop, and a critic subsession
        # resumed mid-flight after a crash. Gated on the callee's explicit
        # ``role: critic``, never inferred from the result's shape.
        if agent.is_critic:
            await self._record_findings(name, output, findings_key)
        return output

    async def _open_subsession(
        self: EngineHost, name: str, subsession_id: str, task_content: str = ""
    ) -> None:
        """Record a subsession takeover: marker, active pointer, and UI divider.

        ``task_content`` is the rendered task brief; it rides the live
        ``subsession.started`` event so the client can show the same task-brief
        card it reconstructs from the seed message on reload.
        """
        display_name = self._display_name(name)
        parent_display = self._display_name(self._session.agent or _GUIDE_AGENT_NAME)
        self._transient.append_marker(
            {
                "type": "subsession_start",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        self._transient.update(
            active_subsession={
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        await self._sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_STARTED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "task": task_content,
                },
            )
        )

    async def _close_subsession(
        self: EngineHost, name: str, subsession_id: str, output: dict[str, object]
    ) -> None:
        """Record a subsession handing control back: marker, clear pointer, divider.

        ``output`` is the sub-agent's structured result; it is stored on the
        ``subsession_end`` marker so a crash-resume replay can return it verbatim.
        """
        self._compactor.clear_subsession_context()
        await self._emitters.emit_context_stats()
        display_name = self._display_name(name)
        parent_display = self._display_name(self._session.agent or _GUIDE_AGENT_NAME)
        # A sub-agent "failed" when it did not return a schema-compliant result
        # (e.g. it ended without calling return_result, so the engine synthesized
        # the {schema_compliance: False} fallback). The flag drives the red
        # <kodo_crit> handback callout in the WebView instead of the green <kodo>.
        failed = output.get(SCHEMA_COMPLIANCE_KEY) is False
        self._transient.append_marker(
            {
                "type": "subsession_end",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
                "failed": failed,
                "result": dict(output),
            }
        )
        self._transient.update(active_subsession=None)
        await self._sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_ENDED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "parent_display_name": parent_display,
                    "failed": failed,
                },
            )
        )

    async def _abort_active_subsession(self: EngineHost) -> None:
        """Close out a subsession a user Stop left open mid-run.

        ``_spawn_subagent`` awaits :meth:`_drive_subsession` then
        :meth:`_close_subsession` as two sequential, unguarded calls (no
        ``try/finally``): when :meth:`~._core.WorkflowEngine.stop` cancels the
        worker task, the cancellation unwinds through ``_drive_subsession``
        and skips ``_close_subsession`` entirely, leaving
        ``_transient.active_subsession`` set, the compactor's subsession gauge
        stale, and the client's collapsible block permanently unclosed (no
        ``subsession_end`` marker/``EVT_SUBSESSION_ENDED`` ever arrives) — see
        ``stop()``, which calls this right after folding the cancellation into
        ``session.jsonl`` and before flipping ``phase`` to ``"stopped"``, so
        the client's ``subsession_ended`` handling lands before its
        ``interrupted`` one.

        Reads the closing agent/display names from ``active_subsession``
        itself (captured correctly at :meth:`_open_subsession` time) rather
        than recomputing from ``self._session.agent`` the way
        ``_close_subsession`` does — by now that field holds the *sub-agent's*
        own name, not its parent's, because ``_drive_subsession`` overwrote it
        and nothing ever restores it before a Stop can land.

        The handback is always marked ``failed`` — a user-initiated Stop is
        neither the clean finish nor the schema-compliance failure the
        ``failed`` flag otherwise distinguishes between, and the generic
        "Interrupted by user" callout that follows right after already tells
        the human what actually happened.
        """
        active = self._transient.active_subsession
        if active is None:
            return
        subsession_id = str(active.get("subsession_id", ""))
        name = str(active.get("agent", ""))
        display_name = str(active.get("display_name") or self._display_name(name))
        parent_display = str(active.get("parent_display_name") or _GUIDE_AGENT_NAME)
        self._compactor.clear_subsession_context()
        await self._emitters.emit_context_stats()
        self._transient.append_marker(
            {
                "type": "subsession_end",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
                "failed": True,
                "result": {},
            }
        )
        self._transient.update(active_subsession=None)
        await self._sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_ENDED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "parent_display_name": parent_display,
                    "failed": True,
                },
            )
        )

    async def _replay_next_subsession(
        self: EngineHost, name: str, findings_key: str = "", phase: str = PHASE_INITIAL
    ) -> dict[str, object]:
        """Consume the next pre-crash subsession marker during resume replay.

        Completed subsessions return their stored structured result immediately
        (the files they wrote are already on disk). The single active
        (un-closed) subsession is rehydrated from its log and driven to
        completion live; once consumed, replay mode ends.
        """
        assert self._replay_subsessions
        rec = self._replay_subsessions.pop(0)
        subsession_id = str(rec["subsession_id"])
        if not self._replay_subsessions:
            self._replay_subsessions = None
        if rec.get("completed"):
            _log.info(
                "Replay: subsession %s already complete; returning stored result", subsession_id
            )
            result = rec.get("result", {})
            return result if isinstance(result, dict) else {}

        _log.info("Replay: resuming active subsession %s (%s)", subsession_id, name)
        rehydrated = [
            Message(role=str(m["role"]), content=m["content"])  # type: ignore[arg-type]
            for m in self._transient.read_subsession_messages(subsession_id)
        ]
        output = await self._drive_subsession(name, subsession_id, rehydrated, findings_key, phase)
        await self._close_subsession(name, subsession_id, output)
        return output

    def _display_name(self: EngineHost, agent_name: str) -> str:
        """User-friendly name for an agent (frontmatter ``display_name`` or derived)."""
        try:
            return self._registry.get(agent_name).display_name or agent_name
        except AgentLoadError:
            return agent_name
