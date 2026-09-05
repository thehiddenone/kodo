"""Unit tests for :mod:`kodo.workproducts` — the reviewable unit of a sub-agent's
output.

A work product is every file one ``run_subagent_<author>`` review loop wrote,
reviewed and accepted together. It replaced ``primary_path``, whose
one-file-per-author model came from the first authors each writing exactly one
document. The properties these cover are the ones the review loop depends on:
identity is stable across *separate* invocations (not just across rounds within
one), membership is per-revision, and what leaves is reported so the caller can
close its findings.
"""

from __future__ import annotations

from pathlib import Path

from kodo.workproducts import (
    WorkProduct,
    read_components,
    read_work_product,
    read_work_products,
    record_components,
    record_membership,
    resolve_needs,
    work_product_for_path,
    work_product_id,
    work_products_log_path,
)


def _record(session_dir: Path, paths: list[str], *, agent: str = "coder", responsibility: str = ""):
    return record_membership(
        session_dir,
        project="proj",
        agent=agent,
        responsibility_code=responsibility,
        paths=paths,
    )


# ---------------------------------------------------------------------------
# work_product_id
# ---------------------------------------------------------------------------


def test_id_is_derived_from_what_produced_the_work() -> None:
    assert work_product_id("proj", "requirements_author") == "proj/requirements_author"
    assert work_product_id("proj", "coder", "LEADERBOARD") == "proj/coder/LEADERBOARD"


def test_id_keeps_projects_apart() -> None:
    """A session may bind several projects; two of them running the same agent
    on the same responsibility must not share a backlog."""
    assert work_product_id("a", "coder", "AUTH") != work_product_id("b", "coder", "AUTH")


def test_id_segments_are_sanitised_because_the_id_becomes_a_file_path() -> None:
    """The first segment is a workspace-folder display name, which is not
    guaranteed filesystem-safe."""
    wp_id = work_product_id("my:proj*name", "coder", "A/B")
    assert ":" not in wp_id and "*" not in wp_id
    assert wp_id.count("/") == 2  # only the segment separators survive


def test_id_without_an_agent_is_empty_rather_than_invented() -> None:
    assert work_product_id("proj", "") == ""
    assert work_product_id("proj", "   ") == ""


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


def test_recording_membership_returns_the_set_and_nothing_removed(tmp_path: Path) -> None:
    work_product, removed = _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])

    assert work_product.id == "proj/coder"
    assert work_product.paths == ("proj/src/a.py", "proj/src/b.py")
    assert removed == ()


def test_membership_order_is_preserved_and_duplicates_dropped(tmp_path: Path) -> None:
    """The author is asked to list its entry point first — that order is
    meaningful and the UI leads with it — while a repeat is a model slip."""
    work_product, _ = _record(tmp_path, ["proj/src/b.py", "proj/src/a.py", "proj/src/b.py"])
    assert work_product.paths == ("proj/src/b.py", "proj/src/a.py")


def test_identity_is_stable_across_separate_invocations(tmp_path: Path) -> None:
    """The crux: the Guide routinely calls the same author several times on the
    same subject ("continue resolving outstanding findings"), and the backlog
    has to survive that. A per-loop minted id would silently orphan it."""
    first, _ = _record(tmp_path, ["proj/src/a.py"])
    second, _ = _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])

    assert first.id == second.id
    assert read_work_product(tmp_path, first.id) is not None


def test_a_file_that_leaves_the_set_is_reported_as_removed(tmp_path: Path) -> None:
    """What the caller needs in order to auto-close that file's findings: an
    outstanding finding against a file nothing will re-read can never be
    verified fixed."""
    _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])
    work_product, removed = _record(tmp_path, ["proj/src/a.py", "proj/src/c.py"])

    assert removed == ("proj/src/b.py",)
    assert work_product.paths == ("proj/src/a.py", "proj/src/c.py")


def test_a_file_added_later_is_not_reported_as_removed(tmp_path: Path) -> None:
    _record(tmp_path, ["proj/src/a.py"])
    _work_product, removed = _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])
    assert removed == ()


def test_responsibility_code_separates_two_components_of_one_agent(tmp_path: Path) -> None:
    leaderboard, _ = _record(tmp_path, ["proj/src/l.py"], responsibility="LEADERBOARD")
    identity, _ = _record(tmp_path, ["proj/src/i.py"], responsibility="PLAYER_IDENTITY")

    assert leaderboard.id != identity.id
    assert {wp.id for wp in read_work_products(tmp_path)} == {leaderboard.id, identity.id}


def test_recording_without_an_agent_is_refused(tmp_path: Path) -> None:
    try:
        record_membership(
            tmp_path, project="proj", agent="", responsibility_code="", paths=["proj/a.py"]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a work product with no authoring agent must be refused")


# ---------------------------------------------------------------------------
# the reverse lookup
# ---------------------------------------------------------------------------


def test_a_file_maps_back_to_the_work_product_that_holds_it(tmp_path: Path) -> None:
    """``guided_dev_status`` asks this for every tracked file: a file's findings
    live under its work product, not under its own path."""
    work_product, _ = _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])

    assert work_product_for_path(tmp_path, "proj/src/b.py") is not None
    assert work_product_for_path(tmp_path, "proj/src/b.py").id == work_product.id  # type: ignore[union-attr]


def test_a_file_that_left_no_longer_maps_to_it(tmp_path: Path) -> None:
    _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])
    _record(tmp_path, ["proj/src/a.py"])

    assert work_product_for_path(tmp_path, "proj/src/b.py") is None


def test_an_unknown_or_empty_path_maps_to_nothing(tmp_path: Path) -> None:
    _record(tmp_path, ["proj/src/a.py"])
    assert work_product_for_path(tmp_path, "proj/src/never.py") is None
    assert work_product_for_path(tmp_path, "") is None


def test_the_last_recorded_membership_wins_an_ambiguous_file(tmp_path: Path) -> None:
    """Nothing structurally stops two authors writing the same file; the replay
    is last-write-wins everywhere else, so this is too."""
    _record(tmp_path, ["proj/src/shared.py"], agent="coder")
    later, _ = _record(tmp_path, ["proj/src/shared.py"], agent="test_coder")

    assert work_product_for_path(tmp_path, "proj/src/shared.py").id == later.id  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_state_is_a_replay_of_the_log_not_an_index(tmp_path: Path) -> None:
    _record(tmp_path, ["proj/src/a.py"])
    _record(tmp_path, ["proj/src/a.py", "proj/src/b.py"])

    # Cold read — nothing in memory carries over.
    work_products = read_work_products(tmp_path)
    assert len(work_products) == 1
    assert work_products[0].paths == ("proj/src/a.py", "proj/src/b.py")


def test_reading_before_anything_is_recorded_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_work_products(tmp_path) == []
    assert read_work_product(tmp_path, "proj/coder") is None
    assert work_product_for_path(tmp_path, "proj/src/a.py") is None


def test_the_log_lives_in_the_projects_kodo_dir(tmp_path: Path) -> None:
    """Project-scoped, not session-scoped: "the architecture is these files" is
    a fact about the project, so a new session on an existing tree must still
    resolve it. Findings stay session-scoped — a backlog really is a judgment
    one session's critics made."""
    assert work_products_log_path(tmp_path) == tmp_path / ".kodo" / "workproducts.jsonl"
    _record(tmp_path, ["proj/src/a.py"])
    assert work_products_log_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# roles and resolution
# ---------------------------------------------------------------------------


def test_roles_narrow_a_work_product_to_the_files_filling_one_role(tmp_path: Path) -> None:
    """One agent may fill several roles from one run — narrative_author writes
    both the Narrative and the Tech Stack — so a consumer asking for one must
    not receive both."""
    work_product, _ = record_membership(
        tmp_path,
        project="proj",
        agent="narrative_author",
        responsibility_code="",
        paths=["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        roles={
            "narrative": ["proj/specs/narrative.md"],
            "tech_stack": ["proj/specs/tech_stack.md"],
        },
    )

    assert work_product.paths == ("proj/specs/narrative.md", "proj/specs/tech_stack.md")
    assert work_product.roles["narrative"] == ("proj/specs/narrative.md",)
    assert read_work_product(tmp_path, work_product.id).roles == work_product.roles  # type: ignore[union-attr]


def test_a_role_naming_a_non_member_is_dropped(tmp_path: Path) -> None:
    """The member set is the authority on what a round wrote; a role pointing
    outside it could hand a consumer a file nobody reviewed."""
    work_product, _ = record_membership(
        tmp_path,
        project="proj",
        agent="architect",
        responsibility_code="",
        paths=["proj/specs/architecture.md"],
        roles={"architecture": ["proj/specs/architecture.md", "proj/specs/ghost.md"]},
    )

    assert work_product.roles == {"architecture": ("proj/specs/architecture.md",)}


def _wp(agent: str, paths: list[str], roles: dict[str, list[str]], **kw) -> WorkProduct:
    return WorkProduct(
        id=work_product_id(kw.get("project", "proj"), agent, kw.get("responsibility", "")),
        project=kw.get("project", "proj"),
        agent=agent,
        responsibility_code=kw.get("responsibility", ""),
        paths=tuple(paths),
        roles={r: tuple(p) for r, p in roles.items()},
    )


def test_global_scope_takes_the_current_work_product_for_a_role() -> None:
    ledger = [_wp("architect", ["proj/a1.md"], {"architecture": ["proj/a1.md"]})]

    [resolved] = resolve_needs(ledger, [("architecture", "global", True)], project="proj")

    assert resolved.paths == ("proj/a1.md",)
    assert resolved.unmet is False


def test_global_scope_takes_the_last_producer_when_a_role_was_refilled() -> None:
    """An author re-invoked on the same subject re-produces its role; the ledger
    is last-write-wins everywhere else, and this is no exception."""
    ledger = [
        _wp("architect", ["proj/old.md"], {"architecture": ["proj/old.md"]}),
        _wp("architect", ["proj/new.md"], {"architecture": ["proj/new.md"]}),
    ]

    [resolved] = resolve_needs(ledger, [("architecture", "global", True)], project="proj")

    assert resolved.paths == ("proj/new.md",)


def test_self_scope_narrows_to_this_spawns_component() -> None:
    ledger = [
        _wp("test_designer", ["proj/t/A.md"], {"test_plan": ["proj/t/A.md"]}, responsibility="A"),
        _wp("test_designer", ["proj/t/B.md"], {"test_plan": ["proj/t/B.md"]}, responsibility="B"),
    ]

    [resolved] = resolve_needs(
        ledger, [("test_plan", "self", True)], project="proj", responsibility_code="B"
    )

    assert resolved.paths == ("proj/t/B.md",)


def test_self_scope_resolves_to_nothing_for_an_unknown_component() -> None:
    ledger = [
        _wp("test_designer", ["proj/t/A.md"], {"test_plan": ["proj/t/A.md"]}, responsibility="A")
    ]

    [resolved] = resolve_needs(
        ledger, [("test_plan", "self", True)], project="proj", responsibility_code="Z"
    )

    assert resolved.paths == ()
    assert resolved.unmet is True


def test_all_scope_unions_every_producer_of_a_role_without_duplicates() -> None:
    ledger = [
        _wp("functional_designer", ["proj/d/A.md"], {"functional_design": ["proj/d/A.md"]}),
        _wp(
            "functional_designer",
            ["proj/d/A.md", "proj/d/B.md"],
            {"functional_design": ["proj/d/A.md", "proj/d/B.md"]},
        ),
    ]

    [resolved] = resolve_needs(ledger, [("functional_design", "all", True)], project="proj")

    assert resolved.paths == ("proj/d/A.md", "proj/d/B.md")


def test_under_review_scope_comes_from_the_round_not_the_ledger() -> None:
    """The engine already knows what it spawned this critic against; looking it
    up again would be a second chance to get it wrong."""
    reviewed = _wp("architect", ["proj/a.md"], {"architecture": ["proj/a.md"]})

    [resolved] = resolve_needs(
        [], [("architecture", "under_review", True)], project="proj", under_review=reviewed
    )

    assert resolved.paths == ("proj/a.md",)


def test_under_review_outside_a_review_round_resolves_to_nothing() -> None:
    [resolved] = resolve_needs([], [("architecture", "under_review", True)], project="proj")
    assert resolved.paths == ()


def test_resolution_ignores_other_projects() -> None:
    ledger = [_wp("architect", ["other/a.md"], {"architecture": ["other/a.md"]}, project="other")]

    [resolved] = resolve_needs(ledger, [("architecture", "global", True)], project="proj")

    assert resolved.paths == ()


def test_an_optional_need_that_resolves_to_nothing_is_not_unmet() -> None:
    [resolved] = resolve_needs([], [("narrative", "global", False)], project="proj")
    assert resolved.paths == ()
    assert resolved.unmet is False


def test_declaration_order_is_preserved() -> None:
    """It is the order the agent's own contract lists its inputs in, and
    therefore the order it expects to read them."""
    needs = [("architecture", "global", True), ("narrative", "global", True)]
    assert [r.role for r in resolve_needs([], needs, project="proj")] == [
        "architecture",
        "narrative",
    ]


def test_a_pre_roles_work_product_reads_as_filling_whatever_is_asked() -> None:
    """Sessions that span the change have work products with no role map.
    Reading those as "all of it" keeps them resolvable instead of silently
    empty, which would look exactly like the bug this replaced."""
    legacy = WorkProduct(
        id="proj/architect",
        project="proj",
        agent="architect",
        responsibility_code="",
        paths=("proj/specs/architecture.md",),
    )

    [resolved] = resolve_needs([legacy], [("architecture", "global", True)], project="proj")

    assert resolved.paths == ("proj/specs/architecture.md",)


def test_an_unwired_scope_resolves_to_nothing_rather_than_raising() -> None:
    """`dependencies` is in the vocabulary but needs the architect's component
    graph. No spec declares it yet; reaching it must not take down a spawn."""
    ledger = [_wp("architect", ["proj/a.md"], {"architecture": ["proj/a.md"]})]

    [resolved] = resolve_needs(ledger, [("architecture", "dependencies", True)], project="proj")

    assert resolved.paths == ()


def test_resolver_scope_names_match_the_spec_vocabulary() -> None:
    """`kodo.workproducts` is a leaf and duplicates the scope names rather than
    importing them, so the two sets are pinned equal here."""
    from kodo.subagents import ALL_SCOPES
    from kodo.workproducts import _resolve

    duplicated = {
        _resolve._SCOPE_GLOBAL,
        _resolve._SCOPE_SELF,
        _resolve._SCOPE_ALL,
        _resolve._SCOPE_UNDER_REVIEW,
    }
    assert duplicated <= ALL_SCOPES


# ---------------------------------------------------------------------------
# the component graph and `dependencies` scope
# ---------------------------------------------------------------------------


def test_the_component_graph_round_trips(tmp_path: Path) -> None:
    record_components(tmp_path, project="proj", components={"LEDGER": [], "AUTH": ["LEDGER"]})
    assert read_components(tmp_path, "proj") == {"LEDGER": [], "AUTH": ["LEDGER"]}


def test_the_latest_graph_wins(tmp_path: Path) -> None:
    """The architect is re-invoked when its document is revised, and the newest
    decomposition is the one later stages must resolve against."""
    record_components(tmp_path, project="proj", components={"AUTH": []})
    record_components(tmp_path, project="proj", components={"AUTH": [], "LEDGER": ["AUTH"]})

    assert read_components(tmp_path, "proj") == {"AUTH": [], "LEDGER": ["AUTH"]}


def test_graphs_are_kept_apart_by_project(tmp_path: Path) -> None:
    record_components(tmp_path, project="proj", components={"AUTH": []})
    record_components(tmp_path, project="other", components={"BILLING": []})

    assert read_components(tmp_path, "proj") == {"AUTH": []}
    assert read_components(tmp_path, "other") == {"BILLING": []}


def test_recording_an_empty_graph_is_a_noop(tmp_path: Path) -> None:
    """It must not shadow a real graph recorded earlier — an architect round
    that escalated before deciding has nothing to say about the decomposition."""
    record_components(tmp_path, project="proj", components={"AUTH": []})
    record_components(tmp_path, project="proj", components={})

    assert read_components(tmp_path, "proj") == {"AUTH": []}


def test_reading_a_graph_that_was_never_recorded_is_empty(tmp_path: Path) -> None:
    assert read_components(tmp_path, "proj") == {}


def _designs() -> WorkProduct:
    """One whole-product functional_designer run: three designs, no responsibility."""
    return WorkProduct(
        id="proj/functional_designer",
        project="proj",
        agent="functional_designer",
        responsibility_code="",
        paths=("proj/d/AUTH.md", "proj/d/LEDGER.md", "proj/d/REPORTS.md"),
        roles={
            "functional_design": (
                "proj/d/AUTH.md",
                "proj/d/LEDGER.md",
                "proj/d/REPORTS.md",
            )
        },
        components={
            "proj/d/AUTH.md": "AUTH",
            "proj/d/LEDGER.md": "LEDGER",
            "proj/d/REPORTS.md": "REPORTS",
        },
    )


def test_self_scope_narrows_a_whole_product_work_product_by_file() -> None:
    """`functional_designer` writes every component's design in one run, so its
    work product carries no responsibility_code. Per-file attribution is the
    only thing that can narrow it to one design."""
    [resolved] = resolve_needs(
        [_designs()],
        [("functional_design", "self", True)],
        project="proj",
        responsibility_code="LEDGER",
    )

    assert resolved.paths == ("proj/d/LEDGER.md",)


def test_dependencies_scope_returns_both_sides_of_an_interface() -> None:
    """AUTH consumes LEDGER, and REPORTS consumes AUTH. A coder working on AUTH
    needs both — an interface has two sides, and changing one without seeing the
    other is how cross-file drift gets written in the first place."""
    graph = {"AUTH": ["LEDGER"], "REPORTS": ["AUTH"], "LEDGER": []}

    [resolved] = resolve_needs(
        [_designs()],
        [("functional_design", "dependencies", False)],
        project="proj",
        responsibility_code="AUTH",
        components=graph,
    )

    assert set(resolved.paths) == {"proj/d/LEDGER.md", "proj/d/REPORTS.md"}


def test_dependencies_scope_excludes_the_component_itself() -> None:
    """That is what `self` is for, and a consumer normally declares both."""
    graph = {"AUTH": ["LEDGER"], "LEDGER": []}

    [resolved] = resolve_needs(
        [_designs()],
        [("functional_design", "dependencies", False)],
        project="proj",
        responsibility_code="AUTH",
        components=graph,
    )

    assert "proj/d/AUTH.md" not in resolved.paths


def test_dependencies_scope_is_empty_for_a_component_with_no_neighbours() -> None:
    """Declared optional precisely so this does not refuse the spawn."""
    [resolved] = resolve_needs(
        [_designs()],
        [("functional_design", "dependencies", False)],
        project="proj",
        responsibility_code="REPORTS",
        components={"REPORTS": [], "AUTH": [], "LEDGER": []},
    )

    assert resolved.paths == ()
    assert resolved.unmet is False


def test_dependencies_scope_without_a_graph_resolves_to_nothing() -> None:
    """No graph means no known relationship — inventing one would be exactly the
    guessing this replaced."""
    [resolved] = resolve_needs(
        [_designs()],
        [("functional_design", "dependencies", False)],
        project="proj",
        responsibility_code="AUTH",
    )

    assert resolved.paths == ()


def test_a_per_component_work_product_needs_no_per_file_attribution() -> None:
    """A per-component stage's whole work product belongs to one component, so
    its own responsibility_code covers every member."""
    plan = WorkProduct(
        id="proj/test_designer/AUTH",
        project="proj",
        agent="test_designer",
        responsibility_code="AUTH",
        paths=("proj/t/AUTH.md",),
        roles={"test_plan": ("proj/t/AUTH.md",)},
    )

    assert plan.component_of("proj/t/AUTH.md") == "AUTH"
    [resolved] = resolve_needs(
        [plan], [("test_plan", "self", True)], project="proj", responsibility_code="AUTH"
    )
    assert resolved.paths == ("proj/t/AUTH.md",)
