"""Behavioral tests for ComponentRegistry.

Tests verify observable outputs (component_dir, display_name, all_codenames)
for given architecture artifact content strings.
"""

from __future__ import annotations

from kodo.workspace import ComponentRegistry

_ARCH_CONTENT = """\
# Architecture

Some prose describing the system.

## Components

| Codename | Display name         |
| -------- | -------------------- |
| AUTH     | User Authentication  |
| TRADE    | Trade Execution      |
| REPORT   | Reporting            |

More prose after the table.
"""


# ---------------------------------------------------------------------------
# component_dir: snake_case derivation
# ---------------------------------------------------------------------------


def test_component_dir_multi_word_display_name() -> None:
    """
    Given an architecture with 'AUTH → User Authentication',
    when component_dir('AUTH') is called,
    then it returns 'user_authentication'.
    """
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.component_dir("AUTH") == "user_authentication"


def test_component_dir_two_word_display_name() -> None:
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.component_dir("TRADE") == "trade_execution"


def test_component_dir_single_word_display_name() -> None:
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.component_dir("REPORT") == "reporting"


def test_component_dir_falls_back_to_codename_when_not_registered() -> None:
    """
    Given a registry built from content that does not mention NOTIFY,
    when component_dir('NOTIFY') is called,
    then the raw codename 'NOTIFY' is returned.
    """
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.component_dir("NOTIFY") == "NOTIFY"


def test_component_dir_on_empty_registry_returns_codename() -> None:
    reg = ComponentRegistry.empty()
    assert reg.component_dir("AUTH") == "AUTH"


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------


def test_display_name_returns_original_casing() -> None:
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.display_name("AUTH") == "User Authentication"


def test_display_name_returns_none_for_unknown_codename() -> None:
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.display_name("UNKNOWN") is None


# ---------------------------------------------------------------------------
# all_codenames
# ---------------------------------------------------------------------------


def test_all_codenames_returns_declared_codes_in_order() -> None:
    """
    Given an architecture with AUTH, TRADE, REPORT in that order,
    when all_codenames() is called,
    then the list matches that order.
    """
    reg = ComponentRegistry(_ARCH_CONTENT)
    assert reg.all_codenames() == ["AUTH", "TRADE", "REPORT"]


def test_all_codenames_empty_on_empty_registry() -> None:
    reg = ComponentRegistry.empty()
    assert reg.all_codenames() == []


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------


def test_skips_rows_with_invalid_codename() -> None:
    """
    Given an architecture table where one row has a lowercase codename,
    when the registry is built,
    then that row is silently ignored.
    """
    content = (
        "| Codename | Display name |\n"
        "| --- | --- |\n"
        "| AUTH | Authentication |\n"
        "| bad  | Bad Component  |\n"
    )
    reg = ComponentRegistry(content)
    assert reg.all_codenames() == ["AUTH"]


def test_handles_missing_architecture_gracefully() -> None:
    reg = ComponentRegistry(None)
    assert reg.all_codenames() == []
    assert reg.component_dir("AUTH") == "AUTH"


def test_display_name_with_special_chars_normalised_to_snake() -> None:
    content = "| Codename | Display name |\n| --- | --- |\n| MKTDATA | Market Data & Feed |\n"
    reg = ComponentRegistry(content)
    assert reg.component_dir("MKTDATA") == "market_data_feed"
