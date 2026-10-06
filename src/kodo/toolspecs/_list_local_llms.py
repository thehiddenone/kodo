"""``list_local_llms`` tool spec — what the local-LLM catalog already serves.

The Model Importer's first call (doc/LLM_REGISTRY.md §4.0b): the families an
imported quant can join, the names and quants already taken, and the context
knobs a catalog entry may name.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["LIST_LOCAL_LLMS"]

LIST_LOCAL_LLMS: ToolSpec = ToolSpec(
    name="list_local_llms",
    external_name="List Local LLMs",
    user_description="List the local LLM catalog",
    description=(
        "List every local-LLM catalog family the registry serves right now — shipped "
        "with Kōdo or added to the user catalog — grouped by `base_llm`. Each family "
        "reports its entries (name, source `shipped`/`user`, repo_id, filename, "
        "quant_type, size_hint, mtp_supported), the `llamacpp_versions` its entries "
        "declare, its `thinking_family` (null when the family has no reasoning tiers) "
        "and the ids of its standalone MTP heads. `context_knobs` lists every YaRN "
        "context-window knob a catalog entry may name, with the llama.cpp "
        "`architecture` it targets and its `native_context`. `user_catalog_dir` is "
        "where new entries are written.\n\n"
        "When to use: before adding quants, to reuse an existing family's `base_llm` "
        "and `llamacpp_version`, to avoid entry names and quants that already exist, "
        "and to see which context knob matches an architecture."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "families": {
                "type": "array",
                "description": "One object per base_llm: base_llm, llamacpp_versions, "
                "thinking_family, mtp_heads, entries.",
            },
            "context_knobs": {
                "type": "array",
                "description": "Context knobs: id, architecture, native_context.",
            },
            "user_catalog_dir": {
                "type": "string",
                "description": "The user catalog root new entries go under.",
            },
        },
        "required": ["families", "context_knobs", "user_catalog_dir"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={
        "families": "hidden",
        "context_knobs": "hidden",
        "user_catalog_dir": "visible",
    },
)
