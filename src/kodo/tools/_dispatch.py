"""Unified tool dispatch — one surface for every agent.

There is no guide-vs-leaf split: every agent (the guide
included) is granted exactly the tools its frontmatter declares, and every
tool call — whoever makes it — is routed through a single
:class:`ToolDispatcher` to the matching :class:`~kodo.tools.Tool` subclass.

:func:`tools_for_agent` resolves an agent's declared tool *names* to the
``ToolSpec`` objects passed to the LLM, skipping any name that has no tool
class here (forward-compatibility with spec-only placeholders).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kodo.toolspecs import (
    ASK_USER,
    CREATE_DIRECTORY,
    CREATE_FILE,
    CREATE_NEW_PROJECT,
    DISABLE_AUTONOMOUS_MODE,
    DOCUMENT_FEEDBACK,
    EDIT_FILE,
    ESCALATE_BLOCKER,
    FILESYSTEM,
    FINALIZE_PROJECT,
    FIND_FILES,
    FIND_TEXT_IN_FILES,
    GET_ROOT_PATHS,
    GET_WEB_SEARCH_STATE,
    GUIDED_DEV_STATUS,
    INIT_PROJECT,
    INTENT_KEY,
    NO_PROJECT_ERROR,
    QUERY_SEARCH_ENGINE,
    READ_ATTACHMENT,
    READ_FILE,
    READ_WEBPAGE,
    REMAINING_TIME,
    RETURN_RESULT,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_COMMAND,
    RUN_SUBAGENT,
    SUBMIT_EVALUATION,
    TOOLCHAIN_BUILD,
    TOOLCHAIN_DEPS,
    UPDATE_WEB_SEARCH_STATE,
    WAIT,
    WEB_SEARCH,
    ToolSpec,
    requires_intent,
)

from ._ask_user import AskUserTool
from ._context import (
    EngineServices,
    GateLike,
    PermissionPartLike,
    SecurityLike,
    SessionLike,
    ToolContext,
)
from ._create_directory import CreateDirectoryTool
from ._create_file import CreateFileTool
from ._create_new_project import CreateNewProjectTool
from ._disable_autonomous_mode import DisableAutonomousModeTool
from ._document_feedback import DocumentFeedbackTool
from ._edit_file import EditFileTool, compute_new_content
from ._edit_review import should_review_edit
from ._escalate_blocker import EscalateBlockerTool
from ._filesystem import FilesystemTool
from ._finalize_project import FinalizeProjectTool
from ._find_files import FindFilesTool
from ._find_text_in_files import FindTextInFilesTool
from ._get_root_paths import GetRootPathsTool
from ._get_web_search_state import GetWebSearchStateTool
from ._guided_dev_status import GuidedDevStatusTool
from ._init_project import InitProjectTool
from ._paths import PathResolver
from ._query_search_engine import QuerySearchEngineTool
from ._read_attachment import ReadAttachmentTool
from ._read_file import ReadFileTool
from ._read_webpage import ReadWebpageTool
from ._remaining_time import RemainingTimeTool
from ._return_result import ReturnResultTool
from ._rollback import RollbackTool
from ._run_author_critic_iteration import RunAuthorCriticIterationTool
from ._run_command import RunCommandTool
from ._run_subagent import RunSubagentTool
from ._submit_evaluation import SubmitEvaluationTool
from ._tool import Tool
from ._toolchain_build import ToolchainBuildTool
from ._toolchain_deps import ToolchainDepsTool
from ._update_web_search_state import UpdateWebSearchStateTool
from ._wait import WaitTool
from ._web_search import WebSearchTool

__all__ = ["DISPATCHABLE_TOOLS_BY_NAME", "ToolDispatcher", "tools_for_agent"]

_log = logging.getLogger(__name__)

# Single source of truth pairing each dispatchable ToolSpec with its Tool class.
# Adding a tool means adding one (spec, Tool-subclass) row here.
_TOOL_CLASSES: tuple[tuple[ToolSpec, type[Tool]], ...] = (
    (READ_FILE, ReadFileTool),
    (READ_ATTACHMENT, ReadAttachmentTool),
    (READ_WEBPAGE, ReadWebpageTool),
    (QUERY_SEARCH_ENGINE, QuerySearchEngineTool),
    (DOCUMENT_FEEDBACK, DocumentFeedbackTool),
    (ESCALATE_BLOCKER, EscalateBlockerTool),
    (ASK_USER, AskUserTool),
    (FILESYSTEM, FilesystemTool),
    (EDIT_FILE, EditFileTool),
    (CREATE_FILE, CreateFileTool),
    (CREATE_DIRECTORY, CreateDirectoryTool),
    (RUN_COMMAND, RunCommandTool),
    (GET_ROOT_PATHS, GetRootPathsTool),
    (FIND_FILES, FindFilesTool),
    (FIND_TEXT_IN_FILES, FindTextInFilesTool),
    (GUIDED_DEV_STATUS, GuidedDevStatusTool),
    (RUN_SUBAGENT, RunSubagentTool),
    (RUN_AUTHOR_CRITIC_ITERATION, RunAuthorCriticIterationTool),
    (RETURN_RESULT, ReturnResultTool),
    (ROLLBACK, RollbackTool),
    (FINALIZE_PROJECT, FinalizeProjectTool),
    (DISABLE_AUTONOMOUS_MODE, DisableAutonomousModeTool),
    (CREATE_NEW_PROJECT, CreateNewProjectTool),
    (INIT_PROJECT, InitProjectTool),
    (TOOLCHAIN_BUILD, ToolchainBuildTool),
    (TOOLCHAIN_DEPS, ToolchainDepsTool),
    (WEB_SEARCH, WebSearchTool),
    (GET_WEB_SEARCH_STATE, GetWebSearchStateTool),
    (UPDATE_WEB_SEARCH_STATE, UpdateWebSearchStateTool),
    (WAIT, WaitTool),
    (REMAINING_TIME, RemainingTimeTool),
    (SUBMIT_EVALUATION, SubmitEvaluationTool),
)

_CLASSES_BY_NAME: dict[str, type[Tool]] = {spec.name: cls for spec, cls in _TOOL_CLASSES}

# Every tool with an implementation, keyed by name. Consumed by tools_for_agent
# to build each agent's LLM-facing tool list.
DISPATCHABLE_TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec, _ in _TOOL_CLASSES}


def tools_for_agent(tool_names: frozenset[str]) -> list[ToolSpec]:
    """Resolve an agent's declared tool names to dispatchable ``ToolSpec``s.

    Names with no tool class (spec-only placeholders) are silently skipped,
    matching the prompt/surface contract.

    Args:
        tool_names: The agent's frontmatter ``tools:`` set (e.g. ``agent.tools``).

    Returns:
        list[ToolSpec]: Specs to pass to the LLM, in catalog order.
    """
    return [
        DISPATCHABLE_TOOLS_BY_NAME[name]
        for name in tool_names
        if name in DISPATCHABLE_TOOLS_BY_NAME
    ]


class ToolDispatcher:
    """Routes one agent run's tool calls to their :class:`Tool` instances.

    One instance is created per agent run (guide or leaf). It owns a
    :class:`~kodo.tools.ToolContext` carrying the injected collaborators plus
    the run's mutable state, and exposes that state (``stop_requested``,
    ``returned_output``) back to the engine after the run.

    The autonomous mode is not passed in: tools read it from
    ``session.effective_autonomous``, which the engine freezes per prompt, so
    one dispatcher serves a prompt regardless of any mode toggle queued mid-run.

    Args:
        resolver: Path resolver for the native file/shell tools (project-confined
            in Guided mode, logical/workspace-folder-keyed in Problem Solver).
        gate: Approval/question gate.
        security: The security layer judging every call before dispatch
            (allow, or ask the user via ``gate.fire_permission``); ``None``
            disables gating.
        session: Session state (carries the frozen ``effective_autonomous``
            and the live ``command_control`` posture the security layer reads).
        services: Engine-side operations (sub-agent launch, author/critic
            iteration, rollback, mode disable, project creation).
        agent_name: Name of the running agent.
        session_id: Session ID for this run.
        mode: The run's workflow mode (``"guided"``/``"problem_solving"``).
        output_schema: The running sub-agent's ``output_schema`` (from its
            ``SubAgentSpec``), so ``return_result`` can validate its result.
            ``None`` for entry agents that never call ``return_result``.
        deadline: Unix timestamp this run must wrap up by, or ``None`` if
            untimed. Populated only for the ``web_search`` agent's dispatcher;
            see :attr:`ToolContext.deadline`.
    """

    __ctx: ToolContext

    def __init__(
        self,
        *,
        resolver: PathResolver,
        gate: GateLike,
        session: SessionLike,
        services: EngineServices,
        agent_name: str,
        session_id: str,
        security: SecurityLike | None = None,
        mode: str = "problem_solving",
        util_paths: dict[str, Path] | None = None,
        output_schema: dict[str, object] | None = None,
        deadline: float | None = None,
    ) -> None:
        self.__ctx = ToolContext(
            resolver=resolver,
            gate=gate,
            security=security,
            session=session,
            services=services,
            agent_name=agent_name,
            session_id=session_id,
            mode=mode,
            util_paths=dict(util_paths or {}),
            output_schema=output_schema,
            deadline=deadline,
        )

    @property
    def stop_requested(self) -> bool:
        """``True`` once the agent called ``escalate_blocker`` or ``return_result``."""
        return self.__ctx.stop_requested

    @property
    def returned_output(self) -> dict[str, object] | None:
        """The normalized result the agent passed to ``return_result``, if any."""
        return self.__ctx.returned_output

    async def dispatch(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        tool_use_id: str = "",
        recovered: bool = False,
    ) -> str:
        """Route one tool call to its handler and return a JSON-encoded result.

        Enforces the mutating-tool ``intent`` contract first — a spec that
        requires ``intent`` is rejected without a non-blank one — then asks
        the security layer to judge the call (an ``ask`` verdict surfaces a
        ``prompt.permission`` gate; a user denial returns an error result
        without dispatching), and finally instantiates the matching
        :class:`Tool` subclass bound to this run's context and invokes its
        :meth:`Tool.handle`.

        Args:
            tool_name: Tool name from :data:`DISPATCHABLE_TOOLS_BY_NAME`.
            tool_input: Parsed JSON input from the LLM tool-use block.
            tool_use_id: The calling ``tool_use`` block's id, exposed to the
                handler via ``ToolContext.current_tool_use_id`` (empty when the
                caller has none).
            recovered: ``True`` when this call was *salvaged* from a model that
                emitted it as plain text (the tool was inferred from the
                argument shape). Outside autonomous mode it forces a permission
                prompt so the user can reject a wrong guess — see
                :meth:`__security_gate` and doc/SECURITY.md §7.

        Returns:
            str: JSON-encoded result returned to the LLM as a tool result.
        """
        self.__ctx.current_tool_use_id = tool_use_id
        tool_cls = _CLASSES_BY_NAME.get(tool_name)
        if tool_cls is None:
            _log.warning(
                "ToolDispatcher: unknown tool %r from %s", tool_name, self.__ctx.agent_name
            )
            return json.dumps({"error": f"Unknown tool: {tool_name!r}"})
        spec = DISPATCHABLE_TOOLS_BY_NAME[tool_name]
        # Generic gate for tools that need a bound project/workspace: reject
        # before dispatch rather than let each tool discover this itself,
        # unless the call is scoped to the private scratch directory
        # (`temporary: true`), which never needs a project.
        if spec.requires_project and not self.__ctx.has_workspace and not tool_input.get(
            "temporary"
        ):
            return json.dumps({"error": NO_PROJECT_ERROR})
        # Generic gate for content-mutating tools: a spec that requires `intent`
        # never dispatches without a non-blank one (the security layer judges
        # calls by it), regardless of what the LLM API let through.
        if requires_intent(spec):
            intent = tool_input.get(INTENT_KEY)
            if not isinstance(intent, str) or not intent.strip():
                return json.dumps(
                    {
                        "error": (
                            f"'{INTENT_KEY}' is required: state in one sentence what this "
                            f"{tool_name} call changes and why, then retry."
                        )
                    }
                )
        denial = await self.__security_gate(tool_name, tool_input, tool_use_id, recovered)
        if denial is not None:
            return denial
        if tool_name in (CREATE_FILE.name, EDIT_FILE.name):
            # Independent of and always evaluated after the security gate —
            # Command Control and Edit Control are orthogonal settings, so
            # the same call may ask twice (security first, then review).
            review_denial = await self.__edit_review_gate(tool_name, tool_input, tool_use_id)
            if review_denial is not None:
                return review_denial
        # run_command and web_search are the only calls the client animates a
        # timeout bar for; tell it execution is genuinely starting now, past
        # whatever judging round or permission wait the gate above may have
        # taken (the "Waiting for tool output" / "Web Search" elapsed clock
        # must start here, not when the card first appeared — see
        # doc/SECURITY.md §6, doc/WEB_SEARCH.md §6). ``services`` is None only
        # in tests that don't wire it (never in production, where the engine
        # always injects a real EngineServices).
        if tool_name in (RUN_COMMAND.name, WEB_SEARCH.name) and self.__ctx.services is not None:
            await self.__ctx.services.notify_tool_call_in_progress(tool_use_id)
        return await tool_cls(self.__ctx).handle(tool_input)

    async def __security_gate(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        tool_use_id: str,
        recovered: bool = False,
    ) -> str | None:
        """Judge the call via the security layer; prompt the user on ``ask``.

        Returns ``None`` when dispatch may proceed (allowed outright, or the
        user granted permission), or a JSON-encoded error result when the user
        denied the call.

        A *recovered* call (salvaged from a model that emitted it as plain
        text — see :meth:`dispatch`) forces the prompt whenever the run is not
        autonomous, regardless of the security verdict: the tool name was
        inferred from the argument shape, so the user gets to reject a wrong
        guess before it runs. In autonomous mode it just runs, exactly like any
        other call the security layer would allow.
        """
        ctx = self.__ctx
        spec = DISPATCHABLE_TOOLS_BY_NAME[tool_name]
        force_ask = recovered and not ctx.session.effective_autonomous

        decision_action = "allow"
        decision_reason = ""
        parts: tuple[PermissionPartLike, ...] = ()
        if ctx.security is not None:
            decision = await ctx.security.evaluate(
                tool_name=tool_name,
                tool_input=tool_input,
                command_control=ctx.session.command_control,
                autonomous=ctx.session.effective_autonomous,
                default_cwd=str(ctx.resolver.default_cwd),
                roots=tuple(rp.path for rp in ctx.root_paths),
                session_rules=ctx.session.security_rules,
                session_path_rules=ctx.session.security_path_rules,
            )
            decision_action = decision.action
            decision_reason = decision.reason
            parts = decision.parts

        if not force_ask and decision_action != "ask":
            return None

        if force_ask:
            reason = (
                "Kōdo recovered a malformed tool call: the model emitted it as plain text "
                "instead of a proper tool call, so the tool was inferred from the arguments "
                "below. Review them before allowing it to run."
            )
            if decision_action == "ask" and decision_reason:
                reason = f"{reason} Security also flagged it: {decision_reason}"
        else:
            reason = decision_reason

        intent_raw = tool_input.get(INTENT_KEY)
        response = await ctx.gate.fire_permission(
            tool_call_id=tool_use_id,
            tool_name=tool_name,
            external_name=spec.external_name,
            risk=spec.security_impact.label,
            intent=intent_raw if isinstance(intent_raw, str) else "",
            reason=reason,
            params=_permission_params(spec, tool_input),
            recovered=force_ask,
            parts=parts,
        )
        if response.action == "allow":
            _log.info("security: user ALLOWED %s (%s)", tool_name, ctx.agent_name)
            # `zip` truncates to the shorter side — a short/malformed
            # `remember` from the client just grants fewer rules, never more;
            # only a shape the server itself offered can ever be granted.
            for part, scope in zip(parts, response.remember, strict=False):
                if part.rule_offer is not None and scope in ("session", "global"):
                    if part.kind == "path":
                        await ctx.services.add_security_path_rule(scope, *part.rule_offer)
                    else:
                        await ctx.services.add_security_rule(scope, *part.rule_offer)
                    _log.info(
                        "security: %s granted %s %s rule for %r (%s)",
                        scope,
                        tool_name,
                        part.kind,
                        part.rule_offer,
                        ctx.agent_name,
                    )
            return None
        _log.info("security: user DENIED %s (%s)", tool_name, ctx.agent_name)
        feedback = response.feedback.strip()
        detail = (
            f" The user's feedback: {feedback}"
            if feedback
            else (
                " No feedback was given — reconsider the approach or consult "
                "the user before retrying."
            )
        )
        return json.dumps(
            {"error": f"The user DENIED permission for this {tool_name} call.{detail}"}
        )

    async def __edit_review_gate(
        self, tool_name: str, tool_input: dict[str, object], tool_use_id: str
    ) -> str | None:
        """Judge a ``create_file``/``edit_file`` call against Edit Control;
        surface a review prompt when it applies.

        Returns ``None`` when dispatch may proceed — Edit Control skips this
        call (``allow_all``, a ``temporary`` call, a ``smart`` call outside
        every heuristic-matched path, or a call that's going to fail
        regardless — file-already-exists, no-op edit, ambiguous/missing
        ``old_string``, unresolvable path — in which case ``handle()`` is
        left to raise its own, identical error rather than showing a review
        for a doomed call) — or a JSON-encoded ``rejected``/
        ``rejected_with_feedback`` result when the user declines it,
        short-circuiting dispatch so ``handle()`` never runs and nothing is
        written.

        Always evaluated *after* :meth:`__security_gate`: Command Control and
        Edit Control are independent settings, so the same call can ask
        twice (security first, then review).
        """
        ctx = self.__ctx
        edit_control = ctx.session.edit_control
        path = str(tool_input.get("path", ""))

        if edit_control == "allow_all" or bool(tool_input.get("temporary")):
            return None

        old_string = ""
        new_string = ""
        if tool_name == EDIT_FILE.name:
            old_string = str(tool_input.get("old_string", ""))
            new_string = str(tool_input.get("new_string", ""))
            if old_string == "" or old_string == new_string:
                return None  # let handle() produce its usual validation error

        try:
            resolved = ctx.resolver.resolve(path)
        except PermissionError:
            return None  # out-of-workspace: let handle() raise its own, identical error

        if tool_name == CREATE_FILE.name:
            if resolved.exists():
                return None  # let handle() raise its own FileExistsError
            mode, old_content, new_content = "new_file", "", str(tool_input.get("content", ""))
        else:
            if not resolved.exists():
                return None  # let handle() raise its own FileNotFoundError
            try:
                old_content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None  # let handle() produce its own, identical error
            try:
                new_content = compute_new_content(path, old_content, old_string, new_string)
            except ValueError:
                return None  # let handle() produce its usual validation error
            mode = "modification"

        if edit_control == "smart" and not should_review_edit(resolved, ctx.root_paths):
            return None

        response = await ctx.gate.fire_edit_review(
            tool_call_id=tool_use_id,
            tool_name=tool_name,
            path=path,
            mode=mode,
            old_content=old_content,
            new_content=new_content,
        )
        if response.action == "approve":
            _log.info("edit_review: user APPROVED %s (%s)", tool_name, ctx.agent_name)
            return None

        _log.info("edit_review: user REJECTED %s (%s)", tool_name, ctx.agent_name)
        result: dict[str, object] = {
            "status": "rejected_with_feedback" if response.feedback else "rejected",
            "path": path,
        }
        if response.feedback:
            result["feedback"] = [
                (
                    {"general_feedback": True, "feedback": entry.feedback}
                    if entry.general_feedback
                    else {
                        "general_feedback": False,
                        "line_from": entry.line_from,
                        "line_to": entry.line_to,
                        "targeted_code": entry.targeted_code,
                        "feedback": entry.feedback,
                    }
                )
                for entry in response.feedback
            ]
        return json.dumps(result)


# Cap for permission-prompt parameter previews; the full value is visible in
# the tool call's own detail box, the prompt only needs enough to decide.
_PERMISSION_VALUE_CHARS = 400


def _permission_params(spec: ToolSpec, tool_input: dict[str, object]) -> list[dict[str, str]]:
    """Customer-visible ``{"name", "value"}`` rows for a permission prompt.

    Projects the input through the spec's ``input_visibility`` map (hidden
    properties never reach the prompt; ``intent`` is carried separately),
    truncating long values.
    """
    rows: list[dict[str, str]] = []
    properties = spec.input_schema.get("properties")
    names = properties.keys() if isinstance(properties, dict) else tool_input.keys()
    for name in names:
        if name == INTENT_KEY or name not in tool_input:
            continue
        if spec.input_visibility.get(name, "hidden") == "hidden":
            continue
        value = tool_input[name]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > _PERMISSION_VALUE_CHARS:
            text = text[:_PERMISSION_VALUE_CHARS] + f"… [{len(text)} chars total]"
        rows.append({"name": name, "value": text})
    return rows
