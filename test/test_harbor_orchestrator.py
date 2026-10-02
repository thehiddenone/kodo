"""``kodo-harbor`` (doc/HARBOR.md): choosing tasks, checking the model, the Harbor
job it writes, and the report it reads back.

The job document is checked against Harbor's own ``JobConfig`` and the Kodo
row is built into a real ``KodoAgent`` by Harbor's factory — the contract
between the orchestrator (no Harbor import) and the adapter (Harbor's process).
Nothing here starts Docker, Harbor or a llama-server.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import cast

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.job.config import JobConfig

from kodo.harbor import (
    BUILTIN_SUITES_DIR,
    CONTAINER_HOST,
    HARBOR_VERSION,
    KODO_AGENT_IMPORT_PATH,
    TERMINUS_2,
    BenchModel,
    HarborInvocation,
    HarborRunError,
    JobPlan,
    JobSummary,
    KodoInstall,
    LlamaAccess,
    Selection,
    SuiteCatalog,
    main,
    sign_test_p_value,
)
from kodo.harbor.agent import KodoAgent, KodoAgentOptions
from kodo.headless import vendor_credential_env_names
from kodo.llms import (
    UNLIMITED_THINKING_TIER,
    LocalLLMEntry,
    add_local_entry,
    get_cloud_registry,
    get_local_registry,
    local_thinking_tiers,
)

_SECRET = "sk-ant-test-secret-value"


def _cloud_model_id(vendor: str) -> str:
    """A model id the registry really has — never a hardcoded guess."""
    return get_cloud_registry()[vendor][0].model_id


def _local_entry(kodo_dir: Path) -> str:
    return next(iter(get_local_registry(kodo_dir)))


@pytest.fixture
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name in vendor_credential_env_names("anthropic"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SECRET)
    return {"ANTHROPIC_API_KEY": _SECRET}


def _selection(*datasets: str) -> Selection:
    selection = Selection()
    for dataset in datasets:
        selection.add_dataset(dataset)
    return selection


# ---------------------------------------------------------------------------
# Selection and suites
# ---------------------------------------------------------------------------


def test_selection_speaks_harbors_vocabulary(tmp_path: Path) -> None:
    task_dir = tmp_path / "my-task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("", encoding="utf-8")
    dataset_dir = tmp_path / "my-dataset"
    dataset_dir.mkdir()

    selection = Selection()
    selection.add_dataset("terminal-bench@2.0")
    selection.add_dataset("acme/secret-bench@3")
    selection.add_task("harbor/hello-world@latest")
    selection.add_path(task_dir)
    selection.add_path(dataset_dir)
    selection.apply_filters(["fix-*"], ["slow-*"], 5)

    assert selection.datasets == [
        {
            "name": "terminal-bench",
            "version": "2.0",
            "task_names": ["fix-*"],
            "exclude_task_names": ["slow-*"],
            "n_tasks": 5,
        },
        {
            "name": "acme/secret-bench",
            "ref": "3",
            "task_names": ["fix-*"],
            "exclude_task_names": ["slow-*"],
            "n_tasks": 5,
        },
        {
            "path": str(dataset_dir.resolve()),
            "task_names": ["fix-*"],
            "exclude_task_names": ["slow-*"],
            "n_tasks": 5,
        },
    ]
    assert selection.tasks == [
        {"name": "harbor/hello-world", "ref": "latest"},
        {"path": str(task_dir.resolve())},
    ]


def test_a_bare_task_name_is_refused() -> None:
    with pytest.raises(HarborRunError, match="ORG/NAME"):
        Selection().add_task("hello-world")


def test_every_builtin_suite_loads_and_is_valid_for_harbor(tmp_path: Path) -> None:
    suites = SuiteCatalog(tmp_path / "none").all()
    assert {s.name for s in suites} >= {"smoke", "terminal-bench", "terminal-bench-sample"}
    for suite in suites:
        assert suite.path.parent == BUILTIN_SUITES_DIR
        config = JobConfig.model_validate({"datasets": suite.datasets, "tasks": suite.tasks})
        assert config.datasets or config.tasks, suite.name


def test_user_suite_shadows_a_builtin_and_resolves_relative_paths(tmp_path: Path) -> None:
    user_dir = tmp_path / "suites"
    user_dir.mkdir()
    (user_dir / "mine.json").write_text(
        json.dumps({"name": "smoke", "tasks": [{"path": "tasks/one"}]}), encoding="utf-8"
    )

    suite = SuiteCatalog(user_dir).find("smoke")

    assert suite.path == user_dir / "mine.json"
    assert suite.tasks == [{"path": str((user_dir / "tasks/one").resolve())}]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x"},
        {"name": "x", "datasets": [{"name": "d", "bogus": 1}]},
        {"name": "x", "datasets": [{"name": "d", "path": "p"}]},
        {"datasets": [{"name": "d"}]},
        {"name": "x", "tasks": [{"name": "o/t"}], "extra": True},
    ],
)
def test_invalid_suites_are_refused(tmp_path: Path, body: dict[str, object]) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(HarborRunError):
        SuiteCatalog(tmp_path).find(str(path))


def test_unknown_suite_names_the_available_ones(tmp_path: Path) -> None:
    with pytest.raises(HarborRunError, match="smoke"):
        SuiteCatalog(tmp_path).find("nope")


# ---------------------------------------------------------------------------
# The model, checked before anything starts
# ---------------------------------------------------------------------------


def test_cloud_model_forwards_only_templates(tmp_path: Path, cloud_env: dict[str, str]) -> None:
    model_id = _cloud_model_id("anthropic")
    model = BenchModel.resolve(f"anthropic/{model_id}", cloud_env, tmp_path)

    assert model.harbor_model_name == f"anthropic/{model_id}"
    assert model.kodo_env() == {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"}
    assert model.allowed_hosts() == ["api.anthropic.com"]
    assert model.litellm_model() == (
        f"anthropic/{model_id}",
        {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"},
    )


def test_litellm_names_google_models_its_own_way(tmp_path: Path) -> None:
    model_id = _cloud_model_id("google")
    model = BenchModel.resolve(f"google/{model_id}", {"GOOGLE_API_KEY": "g"}, tmp_path)
    assert model.litellm_model() == (f"gemini/{model_id}", {"GEMINI_API_KEY": "${GOOGLE_API_KEY}"})


@pytest.mark.parametrize(
    ("text", "env", "message"),
    [
        ("nosuchvendor/m", {}, "Unknown cloud vendor"),
        ("anthropic/no-such-model", {"ANTHROPIC_API_KEY": "k"}, "has no model"),
        ("local/no-such-entry", {}, "No local model"),
    ],
)
def test_bad_models_fail_before_anything_starts(
    tmp_path: Path, text: str, env: dict[str, str], message: str
) -> None:
    with pytest.raises(HarborRunError, match=message):
        BenchModel.resolve(text, env, tmp_path)


def test_missing_credential_names_the_variables(tmp_path: Path) -> None:
    with pytest.raises(HarborRunError, match="ANTHROPIC_API_KEY"):
        BenchModel.resolve(f"anthropic/{_cloud_model_id('anthropic')}", {}, tmp_path)


def test_runtime_catalog_vendors_accept_any_model_id(tmp_path: Path) -> None:
    model = BenchModel.resolve("openrouter/qwen/qwen3-coder", {"OPENROUTER_API_KEY": "k"}, tmp_path)
    assert model.harbor_model_name == "openrouter/qwen/qwen3-coder"
    assert model.allowed_hosts() == ["openrouter.ai"]


def test_bedrock_allows_its_regional_endpoint(tmp_path: Path) -> None:
    env = {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "s", "AWS_REGION": "eu-west-1"}
    model = BenchModel.resolve("bedrock/us.anthropic.claude-sonnet-4-6", env, tmp_path)
    assert model.allowed_hosts() == ["bedrock-runtime.eu-west-1.amazonaws.com"]
    assert set(model.kodo_env()) == set(env)


# ---------------------------------------------------------------------------
# The Harbor job
# ---------------------------------------------------------------------------


def _kodo_row(config: dict[str, object]) -> dict[str, object]:
    agents = cast(list[dict[str, object]], config["agents"])
    return next(a for a in agents if a.get("import_path") == KODO_AGENT_IMPORT_PATH)


def _build_agents(config: dict[str, object], tmp_path: Path) -> list[object]:
    """Build every agent row the way Harbor does at trial start."""
    job = JobConfig.model_validate(config)
    return [
        AgentFactory.create_agent_from_config(row, logs_dir=tmp_path / f"agent{i}")
        for i, row in enumerate(job.agents)
    ]


def test_cloud_job_is_valid_for_harbor_and_holds_no_secret(
    tmp_path: Path, cloud_env: dict[str, str]
) -> None:
    model = BenchModel.resolve(f"anthropic/{_cloud_model_id('anthropic')}", cloud_env, tmp_path)
    plan = JobPlan(
        job_name="j",
        jobs_dir=tmp_path / "jobs",
        model=model,
        agent="kodo_guide",
        selection=_selection("terminal-bench-sample@2.0"),
        controls=[TERMINUS_2],
        attempts=3,
        concurrency=4,
        thinking_level="high",
        turn_timeout=1200,
        install=KodoInstall(version="0.5.28"),
    )

    config = plan.to_config()

    assert _SECRET not in json.dumps(config)
    row = _kodo_row(config)
    assert row["env"] == {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"}
    assert row["extra_allowed_hosts"] == ["api.anthropic.com"]
    agents = _build_agents(config, tmp_path)
    kodo = agents[0]
    assert isinstance(kodo, KodoAgent)
    options = kodo.options
    assert isinstance(options, KodoAgentOptions)
    assert (options.agent, options.thinking_level, options.turn_timeout_sec) == (
        "kodo_guide",
        "high",
        1200,
    )
    assert kodo.version() == "0.5.28"
    assert kodo.extra_env == {"ANTHROPIC_API_KEY": _SECRET}  # resolved only at trial start
    assert type(agents[1]).__name__ == "Terminus2"
    job = JobConfig.model_validate(config)
    assert (job.n_attempts, job.n_concurrent_trials, job.job_name) == (3, 4, "j")


def test_local_job_attaches_to_the_host_llama(tmp_path: Path) -> None:
    kodo_dir = tmp_path / ".kodo"
    entry = _local_entry(kodo_dir)
    model = BenchModel.resolve(entry, {}, kodo_dir)
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("services: {}\n", encoding="utf-8")
    access = LlamaAccess(entry, 8090, "127.0.0.1", 32768)
    plan = JobPlan(
        job_name="j",
        jobs_dir=tmp_path / "jobs",
        model=model,
        agent="kodo_problem_solver",
        selection=_selection("hello-world@1.0"),
        controls=[TERMINUS_2],
        llama=access,
        registry_file=registry,
        compose_overlays=[overlay],
    )

    config = plan.to_config()

    row = _kodo_row(config)
    kwargs = cast(dict[str, object], row["kwargs"])
    assert kwargs["llama_url"] == f"http://{CONTAINER_HOST}:8090"
    assert kwargs["registry_file"] == "/kodo-host/local-llm-registry.json"
    assert row["model_name"] == f"local/{entry}"
    assert row["extra_allowed_hosts"] == [CONTAINER_HOST]
    environment = cast(dict[str, object], config["environment"])
    assert environment["mounts"] == [
        {
            "type": "bind",
            "source": str(registry),
            "target": "/kodo-host/local-llm-registry.json",
            "read_only": True,
        }
    ]
    assert environment["extra_docker_compose"] == [str(overlay)]
    control = cast(list[dict[str, object]], config["agents"])[1]
    control_kwargs = cast(dict[str, object], control["kwargs"])
    assert control["model_name"] == f"openai/{entry}"
    assert control_kwargs["api_base"] == "http://127.0.0.1:8090/v1"
    assert cast(dict[str, object], control_kwargs["model_info"])["max_input_tokens"] == 32768
    agents = _build_agents(config, tmp_path)
    assert isinstance(agents[0], KodoAgent)


def test_local_model_refuses_a_control_that_runs_in_the_container(tmp_path: Path) -> None:
    kodo_dir = tmp_path / ".kodo"
    entry = _local_entry(kodo_dir)
    with pytest.raises(HarborRunError, match="claude-code"):
        JobPlan(
            job_name="j",
            jobs_dir=tmp_path,
            model=BenchModel.resolve(entry, {}, kodo_dir),
            agent="kodo_problem_solver",
            selection=_selection("hello-world"),
            controls=["claude-code"],
            llama=LlamaAccess(entry, 1, "127.0.0.1", 1),
        )


def test_an_empty_selection_is_refused(tmp_path: Path, cloud_env: dict[str, str]) -> None:
    model = BenchModel.resolve(f"anthropic/{_cloud_model_id('anthropic')}", cloud_env, tmp_path)
    with pytest.raises(HarborRunError, match="Nothing to run"):
        JobPlan(
            job_name="j",
            jobs_dir=tmp_path,
            model=model,
            agent="kodo_problem_solver",
            selection=Selection(),
            controls=[],
        )


def test_harbor_runs_pinned_with_this_kodo(tmp_path: Path) -> None:
    command = HarborInvocation(
        Path("/bin/uv"), tmp_path / "job.json", "/w/py_kodo-1-py3-none-any.whl", ["-y"]
    ).command()
    assert command == [
        "/bin/uv",
        "tool",
        "run",
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "--with",
        "/w/py_kodo-1-py3-none-any.whl",
        "harbor",
        "run",
        "-c",
        str(tmp_path / "job.json"),
        "-y",
    ]


def test_harbor_version_matches_the_dev_pin() -> None:
    import importlib.metadata

    assert importlib.metadata.version("harbor") == HARBOR_VERSION


# ---------------------------------------------------------------------------
# The CLI (dry runs: nothing is started)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.chdir(tmp_path)
    return home


@pytest.mark.skipif(sys.platform == "win32", reason="a shell-script uv stand-in")
def test_dry_run_writes_a_reproducible_job(
    cli_home: Path, cloud_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    model = f"anthropic/{_cloud_model_id('anthropic')}"
    code = main(
        [
            "run",
            "--model",
            model,
            "--suite",
            "smoke",
            "--control",
            TERMINUS_2,
            "--kodo-version",
            "0.5.28",
            "--job-name",
            "dry",
            "--dry-run",
        ]
    )

    assert code == 0
    job = cli_home.parent / "jobs" / "dry" / "kodo-job.json"
    config = json.loads(job.read_text(encoding="utf-8"))
    JobConfig.model_validate(config)
    assert config["n_concurrent_trials"] == 4
    assert config["datasets"] == [{"name": "hello-world", "version": "1.0"}]
    out = capsys.readouterr().out
    assert f"harbor=={HARBOR_VERSION}" in out
    assert "py-kodo==0.5.28" in out


@pytest.mark.skipif(sys.platform == "win32", reason="a shell-script uv stand-in")
def test_dry_run_for_a_local_model_runs_one_trial_at_a_time(cli_home: Path) -> None:
    entry = _local_entry(cli_home / ".kodo")
    code = main(
        [
            "run",
            "--model",
            entry,
            "--dataset",
            "hello-world@1.0",
            "--kodo-version",
            "0.5.28",
            "--job-name",
            "dry",
            "--dry-run",
        ]
    )

    assert code == 0
    config = json.loads(
        (cli_home.parent / "jobs" / "dry" / "kodo-job.json").read_text(encoding="utf-8")
    )
    assert config["n_concurrent_trials"] == 1
    kwargs = _kodo_row(config)["kwargs"]
    assert kwargs["llama_url"] == f"http://{CONTAINER_HOST}:8090"


def _local_entry_where(kodo_dir: Path, tiered: bool) -> str:
    """A catalog entry whose thinking family has the unlimited tier, or none at all."""
    for name, entry in get_local_registry(kodo_dir).items():
        tiers = local_thinking_tiers(entry.base_llm) if entry.base_llm else ()
        if (UNLIMITED_THINKING_TIER in tiers) if tiered else not tiers:
            return name
    raise AssertionError(f"the catalog has no entry with tiered={tiered}")


@pytest.mark.skipif(sys.platform == "win32", reason="a shell-script uv stand-in")
def test_unlimited_thinking_reaches_the_kodo_agent(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = _local_entry_where(cli_home / ".kodo", tiered=True)
    argv = ["run", "--model", entry, "--dataset", "hello-world@1.0", "--kodo-version", "0.5.28"]
    code = main(
        [*argv, "--thinking-level", UNLIMITED_THINKING_TIER, "--job-name", "dry", "--dry-run"]
    )

    assert code == 0
    config = json.loads(
        (cli_home.parent / "jobs" / "dry" / "kodo-job.json").read_text(encoding="utf-8")
    )
    assert _kodo_row(config)["kwargs"]["thinking_level"] == UNLIMITED_THINKING_TIER
    assert f"thinking {UNLIMITED_THINKING_TIER}" in capsys.readouterr().out


def test_a_tier_the_model_lacks_fails_before_the_job_exists(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = _local_entry_where(cli_home / ".kodo", tiered=True)
    argv = ["run", "--model", entry, "--suite", "smoke", "--thinking-level", "no-such-tier"]
    code = main([*argv, "--job-name", "dry", "--dry-run"])

    assert code == 2
    err = capsys.readouterr().err
    assert "--thinking-level 'no-such-tier'" in err
    assert UNLIMITED_THINKING_TIER in err
    assert not (cli_home.parent / "jobs" / "dry").exists()


def test_a_model_without_thinking_tiers_refuses_any_level(cli_home: Path) -> None:
    kodo_dir = cli_home / ".kodo"
    model = BenchModel.resolve(_local_entry_where(kodo_dir, tiered=False), {}, kodo_dir)

    assert model.thinking_tiers == ()
    with pytest.raises(HarborRunError, match="has no thinking tiers"):
        model.check_thinking_level(UNLIMITED_THINKING_TIER)


@pytest.mark.skipif(sys.platform == "win32", reason="a shell-script uv stand-in")
def test_platform_pins_the_task_images_through_a_compose_overlay(
    cli_home: Path, cloud_env: dict[str, str]
) -> None:
    model = f"anthropic/{_cloud_model_id('anthropic')}"
    argv = ["run", "--model", model, "--dataset", "swebench-verified@1.0"]
    argv += ["--platform", "linux/amd64", "--kodo-version", "0.5.28"]
    code = main([*argv, "--job-name", "dry", "--dry-run"])

    assert code == 0
    job = cli_home.parent / "jobs" / "dry"
    config = json.loads((job / "kodo-job.json").read_text(encoding="utf-8"))
    JobConfig.model_validate(config)
    (overlay,) = cast(
        list[str], cast(dict[str, object], config["environment"])["extra_docker_compose"]
    )
    assert Path(overlay).read_text(encoding="utf-8") == (
        'services:\n  main:\n    platform: "linux/amd64"\n'
    )


def test_a_malformed_platform_fails_before_the_job_exists(
    cli_home: Path, cloud_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    model = f"anthropic/{_cloud_model_id('anthropic')}"
    argv = ["run", "--model", model, "--suite", "smoke", "--platform", "amd64"]
    code = main([*argv, "--job-name", "dry", "--dry-run"])

    assert code == 2
    assert "--platform 'amd64'" in capsys.readouterr().err
    assert not (cli_home.parent / "jobs" / "dry").exists()


def test_run_errors_are_reported_not_raised(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--model", "nosuchvendor/x", "--suite", "smoke", "--dry-run"])
    assert code == 2
    assert "Unknown cloud vendor" in capsys.readouterr().err


def test_suites_command_lists_the_builtins(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["suites", "--json"]) == 0
    names = {row["name"] for row in json.loads(capsys.readouterr().out)}
    assert "terminal-bench" in names


def _listed(capsys: pytest.CaptureFixture[str], *argv: str) -> list[dict[str, object]]:
    assert main(["list-models", *argv, "--json"]) == 0
    return cast(list[dict[str, object]], json.loads(capsys.readouterr().out))


def test_list_models_local_shows_only_what_run_can_serve(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kodo_dir = cli_home / ".kodo"
    gguf = cli_home / "mine.gguf"
    gguf.write_bytes(b"GGUF")
    add_local_entry(
        kodo_dir,
        LocalLLMEntry(name="my-gguf", kind="custom_file", description="Mine.", path=str(gguf)),
    )
    add_local_entry(
        kodo_dir,
        LocalLLMEntry(
            name="my-box",
            kind="custom_server_url",
            description="An external server.",
            url="http://192.168.1.50:8042",
        ),
    )

    rows = _listed(capsys, "local")

    # A fresh home has downloaded nothing, so no catalog entry is installed,
    # and a custom_server_url entry is not a model kodo-llama-server launches.
    assert rows == [
        {"model": "my-gguf", "kind": "local", "description": "Mine.", "credential": None}
    ]
    assert BenchModel.resolve("my-gguf", {}, kodo_dir).is_local


def test_list_models_cloud_marks_which_vendors_have_a_credential(
    cli_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = get_cloud_registry()
    for vendor in [*catalog, "openrouter", "bedrock"]:
        for name in vendor_credential_env_names(vendor):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SECRET)

    rows = _listed(capsys, "cloud")

    credential = {row["model"]: row["credential"] for row in rows}
    for vendor, entries in catalog.items():
        for entry in entries:
            assert credential[f"{vendor}/{entry.model_id}"] is (vendor == "anthropic")
    assert credential["openrouter/<MODEL_ID>"] is False
    assert credential["bedrock/<MODEL_ID>"] is False
    assert {row["kind"] for row in rows} == {"cloud"}


def test_list_models_without_a_kind_lists_both(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gguf = cli_home / "mine.gguf"
    gguf.write_bytes(b"GGUF")
    add_local_entry(
        cli_home / ".kodo",
        LocalLLMEntry(name="my-gguf", kind="custom_file", description="Mine.", path=str(gguf)),
    )

    rows = _listed(capsys)

    assert rows[0]["model"] == "my-gguf"
    assert {row["kind"] for row in rows} == {"local", "cloud"}
    assert main(["list-models"]) == 0
    assert "my-gguf" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _trial(
    job: Path,
    name: str,
    task: str,
    agent: str,
    reward: float | None,
    *,
    kodo: dict[str, object] | None = None,
    exception: str | None = None,
) -> None:
    result: dict[str, object] = {
        "trial_name": name,
        "task_name": task,
        "agent_info": {
            "name": agent,
            "version": "1",
            "model_info": {"name": "claude-sonnet-5", "provider": "anthropic"},
        },
        "config": {"agent": {"kwargs": {"agent": "kodo_guide"} if agent == "kodo" else {}}},
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": {"exception_type": exception} if exception else None,
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 40,
            "n_output_tokens": 10,
            "cost_usd": 0.5,
            "metadata": {"kodo": kodo} if kodo is not None else None,
        },
        "agent_execution": {
            "started_at": "2026-09-25T10:00:00+00:00",
            "finished_at": "2026-09-25T10:01:00+00:00",
        },
    }
    (job / name).mkdir(parents=True)
    (job / name / "result.json").write_text(json.dumps(result), encoding="utf-8")


def test_summary_scores_each_arm_and_pairs_kodo_with_the_control(tmp_path: Path) -> None:
    job = tmp_path / "job"
    meta = {
        "outcome": "completed",
        "questions_asked": 1,
        "tool_calls": 7,
        "tool_denials": 2,
        "nudges": 0,
        "per_agent": {
            "kodo_guide": {"calls": 2, "input_tokens": 50, "output_tokens": 5, "usd": 0.2}
        },
    }
    for i, task in enumerate(("a", "b", "c", "d")):
        _trial(job, f"k{i}", task, "kodo", 1.0 if task != "d" else 0.0, kodo=meta)
    _trial(job, "t0", "a", "terminus-2", 0.0)
    _trial(job, "t1", "b", "terminus-2", 0.0)
    _trial(job, "t2", "c", "terminus-2", 1.0)
    _trial(job, "t3", "d", "terminus-2", None, exception="AgentTimeoutError")
    (job / "result.json").write_text(json.dumps({"id": "job"}), encoding="utf-8")

    data = JobSummary.load(job).to_dict()

    arms = cast(dict[str, dict[str, object]], data["arms"])
    kodo = arms["kodo[kodo_guide] @ anthropic/claude-sonnet-5"]
    other = arms["terminus-2 @ anthropic/claude-sonnet-5"]
    assert (kodo["trials"], kodo["solved"], kodo["mean_reward"]) == (4, 3, 0.75)
    assert (other["scored"], other["solved"], other["mean_reward"]) == (3, 1, 0.25)
    assert other["errors"] == {"AgentTimeoutError": 1}
    assert (kodo["input_tokens"], kodo["cache_tokens"], kodo["cost_usd"]) == (400, 160, 2.0)
    assert kodo["mean_agent_seconds"] == 60.0
    details = cast(dict[str, object], kodo["kodo"])
    assert details["outcomes"] == {"completed": 4}
    assert (details["questions_asked"], details["tool_denials"]) == (4, 8)
    per_agent = cast(dict[str, dict[str, float]], details["per_agent"])
    assert per_agent["kodo_guide"]["calls"] == 8
    (pair,) = cast(list[dict[str, object]], data["comparisons"])
    assert (pair["tasks"], pair["kodo_better"], pair["kodo_worse"], pair["tied"]) == (4, 2, 0, 2)
    assert pair["p_value"] == pytest.approx(0.5)
    markdown = JobSummary.load(job).to_markdown()
    assert "kodo[kodo_guide] @ anthropic/claude-sonnet-5" in markdown
    assert "Sandbox denials: 8" in markdown


def test_summarize_command_writes_the_report_next_to_the_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "job"
    _trial(job, "k0", "a", "kodo", 1.0, kodo={"outcome": "completed"})
    assert main(["summarize", str(job)]) == 0
    assert (job / "kodo-summary.md").is_file()
    assert json.loads((job / "kodo-summary.json").read_text(encoding="utf-8"))["arms"]
    assert "Benchmark summary" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("better", "worse", "p"),
    [(0, 0, 1.0), (5, 0, 0.0625), (1, 1, 1.0), (9, 1, 0.021484375), (0, 10, 0.001953125)],
)
def test_sign_test_p_values(better: int, worse: int, p: float) -> None:
    assert sign_test_p_value(better, worse) == pytest.approx(p)
