"""The benchmark report: every arm's score and cost, and Kodo against each control.

Read from a finished (or partly finished) Harbor job directory — one
``<trial>/result.json`` per trial (Harbor's ``TrialResult``) — as plain JSON,
so the report never needs Harbor installed. An *arm* is one agent on one
model: ``kodo[kodo_problem_solver] @ anthropic/claude-sonnet-5``,
``terminus-2 @ anthropic/claude-sonnet-5``.

Per arm: trials, how many the verifier scored, how many it solved (reward
≥ 1), the mean reward (an unscored trial counts as 0 — the task was not
done), errors by exception type, tokens, cost and agent wall time. Kodo arms
also get what only Kodo reports (``agent_result.metadata.kodo``): run
outcomes, questions asked with no user present, sandbox denials, stuck
nudges, and usage per sub-agent.

**Paired comparison.** For each Kodo arm against each other arm, tasks both
ran are compared by their mean reward over attempts: Kodo better, worse, or
tied. The p-value is a two-sided exact sign test over the untied tasks — the
task is the unit, never the attempt, because attempts of one task are not
independent samples.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import cast

__all__ = ["JobSummary", "sign_test_p_value"]

_KODO = "kodo"
_SOLVED = 1.0


def sign_test_p_value(better: int, worse: int) -> float:
    """Two-sided exact sign test.

    Args:
        better (int): Tasks where the first arm scored higher.
        worse (int): Tasks where it scored lower (ties excluded).

    Returns:
        float: The p-value (``1.0`` when there is nothing to test).
    """
    n = better + worse
    if n == 0:
        return 1.0
    k = min(better, worse)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / float(2**n)
    return min(1.0, 2.0 * tail)


class _Trial:
    """The fields of one ``TrialResult`` the report uses."""

    __task: str
    __arm: str
    __is_kodo: bool
    __reward: float | None
    __exception: str | None
    __tokens: tuple[int, int, int]
    __cost: float
    __seconds: float | None
    __kodo: dict[str, object]

    def __init__(self, data: dict[str, object]) -> None:
        self.__task = str(data.get("task_name", ""))
        info = _dict(data.get("agent_info"))
        agent = str(info.get("name", "?"))
        self.__is_kodo = agent == _KODO
        if self.__is_kodo:
            kwargs = _dict(_dict(_dict(data.get("config")).get("agent")).get("kwargs"))
            agent = f"{_KODO}[{kwargs.get('agent', 'kodo_problem_solver')}]"
        model_info = _dict(info.get("model_info"))
        model = "/".join(
            str(part) for part in (model_info.get("provider"), model_info.get("name")) if part
        )
        self.__arm = f"{agent} @ {model or '?'}"
        rewards = _dict(_dict(data.get("verifier_result")).get("rewards"))
        value = rewards.get("reward", next(iter(rewards.values()), None))
        self.__reward = float(value) if isinstance(value, (int, float)) else None
        self.__exception = (
            str(_dict(data.get("exception_info")).get("exception_type"))
            if data.get("exception_info")
            else None
        )
        result = _dict(data.get("agent_result"))
        self.__tokens = (
            _int(result.get("n_input_tokens")),
            _int(result.get("n_cache_tokens")),
            _int(result.get("n_output_tokens")),
        )
        cost = result.get("cost_usd")
        self.__cost = float(cost) if isinstance(cost, (int, float)) else 0.0
        self.__kodo = _dict(_dict(result.get("metadata")).get(_KODO))
        timing = _dict(data.get("agent_execution"))
        self.__seconds = _seconds(timing.get("started_at"), timing.get("finished_at"))

    @property
    def task(self) -> str:
        return self.__task

    @property
    def arm(self) -> str:
        return self.__arm

    @property
    def is_kodo(self) -> bool:
        return self.__is_kodo

    @property
    def reward(self) -> float | None:
        return self.__reward

    @property
    def exception(self) -> str | None:
        return self.__exception

    @property
    def tokens(self) -> tuple[int, int, int]:
        return self.__tokens

    @property
    def cost(self) -> float:
        return self.__cost

    @property
    def seconds(self) -> float | None:
        return self.__seconds

    @property
    def kodo(self) -> dict[str, object]:
        return dict(self.__kodo)


class JobSummary:
    """The report for one Harbor job directory."""

    __job_dir: Path
    __trials: tuple[_Trial, ...]

    def __init__(self, job_dir: Path, trials: list[dict[str, object]]) -> None:
        """Bind the job's trial results.

        Args:
            job_dir (Path): The job directory.
            trials (list[dict[str, object]]): Parsed ``TrialResult`` documents.
        """
        self.__job_dir = job_dir
        self.__trials = tuple(_Trial(t) for t in trials)

    @classmethod
    def load(cls, job_dir: Path) -> JobSummary:
        """Read every trial result under *job_dir*.

        Args:
            job_dir (Path): A Harbor job directory.

        Returns:
            JobSummary: The report (empty when no trial has finished).
        """
        trials: list[dict[str, object]] = []
        for path in sorted(job_dir.glob("*/result.json")):
            try:
                loaded: object = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(loaded, dict) and "trial_name" in loaded:
                trials.append(cast(dict[str, object], loaded))
        return cls(job_dir, trials)

    @property
    def trial_count(self) -> int:
        """Trials with a result."""
        return len(self.__trials)

    def to_dict(self) -> dict[str, object]:
        """The report as JSON-ready data.

        Returns:
            dict[str, object]: ``{"job_dir", "arms", "comparisons"}``.
        """
        return {
            "job_dir": str(self.__job_dir),
            "arms": {arm: self.__arm_stats(arm) for arm in self.__arms()},
            "comparisons": self.__comparisons(),
        }

    def to_markdown(self) -> str:
        """The report for people.

        Returns:
            str: Markdown.
        """
        data = self.to_dict()
        arms = cast(dict[str, dict[str, object]], data["arms"])
        lines = [f"# Benchmark summary — {self.__job_dir.name}", ""]
        if not arms:
            return "\n".join([*lines, "No trial has finished yet.", ""])
        lines += [
            "| Arm | Trials | Solved | Mean reward | Errors "
            "| Tokens in (cached) / out | Cost | Agent time |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for arm, stats in arms.items():
            errors = cast(dict[str, int], stats["errors"])
            error_text = ", ".join(f"{k} ×{v}" for k, v in errors.items()) or "—"
            seconds = stats["mean_agent_seconds"]
            lines.append(
                f"| {arm} | {stats['trials']} | {stats['solved']} "
                f"| {cast(float, stats['mean_reward']):.3f} | {error_text} "
                f"| {stats['input_tokens']:,} ({stats['cache_tokens']:,}) / "
                f"{stats['output_tokens']:,} | ${cast(float, stats['cost_usd']):.2f} "
                f"| {f'{seconds:.0f}s' if isinstance(seconds, float) else '—'} |"
            )
        comparisons = cast(list[dict[str, object]], data["comparisons"])
        if comparisons:
            lines += [
                "",
                "## Kodo against each other arm (paired by task)",
                "",
                "| Kodo | Other | Tasks | Kodo better | Kodo worse | Tied | Sign-test p |",
                "|---|---|---|---|---|---|---|",
            ]
            for c in comparisons:
                lines.append(
                    f"| {c['kodo']} | {c['other']} | {c['tasks']} | {c['kodo_better']} "
                    f"| {c['kodo_worse']} | {c['tied']} | {cast(float, c['p_value']):.3g} |"
                )
        for arm, stats in arms.items():
            kodo = stats.get("kodo")
            if not isinstance(kodo, dict):
                continue
            details = cast(dict[str, object], kodo)
            lines += ["", f"## {arm}", ""]
            outcomes = cast(dict[str, int], details["outcomes"])
            lines.append(
                "- Outcomes: " + ", ".join(f"{k} ×{v}" for k, v in sorted(outcomes.items()))
            )
            for key, label in (
                ("questions_asked", "Questions asked with no user present"),
                ("tool_calls", "Tool calls"),
                ("tool_denials", "Sandbox denials"),
                ("nudges", "Stuck-watchdog nudges"),
            ):
                lines.append(f"- {label}: {details[key]}")
            per_agent = cast(dict[str, dict[str, float]], details["per_agent"])
            if per_agent:
                lines += ["", "| Agent | Calls | Tokens in / out | Cost |", "|---|---|---|---|"]
                for name, row in sorted(per_agent.items(), key=lambda kv: -kv[1]["usd"]):
                    lines.append(
                        f"| {name} | {int(row['calls'])} | {int(row['input_tokens']):,} / "
                        f"{int(row['output_tokens']):,} | ${row['usd']:.2f} |"
                    )
        return "\n".join([*lines, ""])

    # ------------------------------------------------------------------

    def __arms(self) -> list[str]:
        kodo = sorted({t.arm for t in self.__trials if t.is_kodo})
        other = sorted({t.arm for t in self.__trials if not t.is_kodo})
        return [*kodo, *other]

    def __arm_stats(self, arm: str) -> dict[str, object]:
        trials = [t for t in self.__trials if t.arm == arm]
        rewards = [t.reward for t in trials]
        errors: dict[str, int] = {}
        for t in trials:
            if t.exception:
                errors[t.exception] = errors.get(t.exception, 0) + 1
        seconds = [t.seconds for t in trials if t.seconds is not None]
        stats: dict[str, object] = {
            "trials": len(trials),
            "scored": sum(r is not None for r in rewards),
            "solved": sum(r is not None and r >= _SOLVED for r in rewards),
            "mean_reward": sum(r or 0.0 for r in rewards) / len(trials),
            "errors": dict(sorted(errors.items())),
            "input_tokens": sum(t.tokens[0] for t in trials),
            "cache_tokens": sum(t.tokens[1] for t in trials),
            "output_tokens": sum(t.tokens[2] for t in trials),
            "cost_usd": round(sum(t.cost for t in trials), 6),
            "mean_agent_seconds": sum(seconds) / len(seconds) if seconds else None,
        }
        if trials[0].is_kodo:
            stats["kodo"] = _kodo_stats(trials)
        return stats

    def __comparisons(self) -> list[dict[str, object]]:
        by_arm: dict[str, dict[str, list[float]]] = {}
        for t in self.__trials:
            by_arm.setdefault(t.arm, {}).setdefault(t.task, []).append(t.reward or 0.0)
        kodo_arms = sorted({t.arm for t in self.__trials if t.is_kodo})
        rows: list[dict[str, object]] = []
        for kodo in kodo_arms:
            for other in self.__arms():
                if other == kodo:
                    continue
                common = sorted(set(by_arm[kodo]) & set(by_arm[other]))
                better = worse = 0
                for task in common:
                    mine, theirs = _mean(by_arm[kodo][task]), _mean(by_arm[other][task])
                    if mine > theirs:
                        better += 1
                    elif mine < theirs:
                        worse += 1
                rows.append(
                    {
                        "kodo": kodo,
                        "other": other,
                        "tasks": len(common),
                        "kodo_better": better,
                        "kodo_worse": worse,
                        "tied": len(common) - better - worse,
                        "p_value": sign_test_p_value(better, worse),
                    }
                )
        return rows


def _kodo_stats(trials: list[_Trial]) -> dict[str, object]:
    outcomes: dict[str, int] = {}
    totals = {"questions_asked": 0, "tool_calls": 0, "tool_denials": 0, "nudges": 0}
    per_agent: dict[str, dict[str, float]] = {}
    for t in trials:
        meta = t.kodo
        outcome = str(meta.get("outcome", "no_record"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        for key in totals:
            totals[key] += _int(meta.get(key))
        for name, raw in _dict(meta.get("per_agent")).items():
            row = per_agent.setdefault(
                name, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
            )
            values = _dict(raw)
            for key in row:
                value = values.get(key)
                if isinstance(value, (int, float)):
                    row[key] += value
    return {"outcomes": outcomes, **totals, "per_agent": per_agent}


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _seconds(started: object, finished: object) -> float | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return None
    return delta.total_seconds()
