"""``KodoAgentOptions`` — the ``--agent-kwarg`` surface of the Harbor adapter.

Every field carries a ``description`` so ``harbor agent schema
kodo.harbor.agent:KodoAgent`` documents itself. ``kodo-harbor`` writes these
as ``agents[].kwargs`` in the job config it generates; a hand-written Harbor
job may set them the same way (doc/HARBOR.md §4).
"""

from __future__ import annotations

from harbor.agents.options import InstalledAgentOptions
from pydantic import Field

__all__ = ["DEFAULT_TOP_AGENT", "KodoAgentOptions"]

#: The top-level agent a trial runs when ``agent`` is not given.
DEFAULT_TOP_AGENT = "kodo_problem_solver"


class KodoAgentOptions(InstalledAgentOptions):
    """Harbor kwargs for :class:`~._agent.KodoAgent`.

    ``version`` (inherited) is the ``py-kodo`` version installed in the task
    container; it defaults to the adapter's own version, so the container
    runs the same kodo as the host that built the job.
    """

    agent: str = Field(
        default=DEFAULT_TOP_AGENT,
        description="Kodo top-level agent the trial runs (a built-in kodo_* or a user agent).",
    )
    llama_url: str | None = Field(
        default=None,
        description=(
            "Local model only: the host llama-server as the container reaches it, "
            "e.g. http://host.docker.internal:8090."
        ),
    )
    registry_file: str | None = Field(
        default=None,
        description=(
            "Local model only: container path of the host's local-llm-registry.json, "
            "mounted read-only (it carries the active profile, so host and container "
            "agree on the context window)."
        ),
    )
    thinking_level: str | None = Field(
        default=None, description="Thinking tier for the model's family (kodo's tier slugs)."
    )
    turn_timeout_sec: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Kodo-side bound on the one autonomous turn. Omit to let Harbor's own agent "
            "timeout govern, as it does for every other agent."
        ),
    )
    kodo_wheel: str | None = Field(
        default=None,
        description="Host path of a py-kodo wheel to install instead of the PyPI release.",
    )
    agents_dir: str | None = Field(
        default=None,
        description=(
            "Host path of user-installed agents (~/.kodo/agents) to install in the container; "
            "needed only for a non-built-in agent."
        ),
    )
    python_version: str = Field(
        default="3.12", description="Python uv installs for kodo (py-kodo needs 3.12 or later)."
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Sandbox root inside the container: the only directory the run may change. "
            "Defaults to the task's working directory."
        ),
    )
