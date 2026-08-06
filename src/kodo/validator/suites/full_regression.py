"""Suite: the full regression battery — every shipped scenario, one suite.

Selector: ``full_regression`` (or ``all``).

Pairs each shipped LLM-under-test with the scenario(s) originally built for
it (before scenarios lost their LLM pins — see :mod:`kodo.validator._scenario`
and :mod:`kodo.validator._suite`), and judges every entry with the model every
scenario file already assumed as its VLLM. Running this suite reproduces
today's per-family LUT pinning exactly, just batched and reported together —
plus the suite's cross-LLM summary round at the end.
"""

from __future__ import annotations

from collections.abc import Sequence

from kodo.validator import LLMUnderTest, SuiteEntry, ValidationSuite
from kodo.validator.prompts import PROMPTS
from kodo.validator.scenarios import resolve_selectors

# Every shipped scenario already assumed this as its VLLM/judge.
_JUDGE = "unsloth-qwen36-27b-q8-k-xl"

_QWEN35_9B = LLMUnderTest(llm="unsloth-qwen35-9b-q8-k-xl")
# The capable 27B model plays both LUT and judge for tictactoe_sparse_task —
# see that scenario file's docstring for why (ask_user needs a model that
# reliably uses it).
_QWEN36_27B = LLMUnderTest(llm=_JUDGE)
_ORNITH10_35B_A3B = LLMUnderTest(llm="deepreinforce-ornith10-35b-a3b-bf16")
# The sampling preset this scenario was built to validate (doc/QUANT_SAMPLING.md).
_LAGUNA_S_2_1 = LLMUnderTest(
    llm="unsloth-laguna-s-2-1-mxfp4-moe",
    flavor="unsloth-laguna-s-2-1-mxfp4-moe-light-tail-cull",
)

# Every ``toolchain_<language>`` scenario (grouped under the
# ornith10-35b-a3b/ sub-directory before scenarios were flattened into one
# package).
_TOOLCHAIN_LANGUAGES = (
    "c",
    "cpp",
    "csharp",
    "go",
    "java",
    "javascript",
    "kotlin",
    "python",
    "ruby",
    "rust",
    "swift",
    "typescript",
)


def _entries_for(lut: LLMUnderTest, selectors: Sequence[str]) -> list[SuiteEntry]:
    """Every scenario the selectors resolve to, each paired with the same LUT.

    Args:
        lut (LLMUnderTest): The LLM + flavor to pair with every match.
        selectors (Sequence[str]): :mod:`kodo.validator.scenarios` selectors.

    Returns:
        list[SuiteEntry]: One entry per resolved scenario.
    """
    return [SuiteEntry(llm_under_test=lut, scenario=s) for _, s in resolve_selectors(selectors)]


SUITE = ValidationSuite(
    name="full-regression",
    entries=[
        *_entries_for(_QWEN35_9B, ["tictactoe_detailed_task"]),
        *_entries_for(_QWEN36_27B, ["tictactoe_sparse_task"]),
        *_entries_for(_ORNITH10_35B_A3B, [f"toolchain_{lang}" for lang in _TOOLCHAIN_LANGUAGES]),
        *_entries_for(_LAGUNA_S_2_1, ["attachment_report"]),
    ],
    judge_llm=_JUDGE,
    summary_prompt=PROMPTS.get("suite_summary/default"),
)
