"""``set_mtp_heads`` tool spec — add standalone MTP draft heads to a catalog family.

Backs the Model Importer (doc/LLM_REGISTRY.md §4.0b). Writes the family's
``~/.kodo/local_llms/<base_llm>/mtp_sidecars.json`` (format: §4.0a), which
replaces the shipped list for that family — so the tool always carries the
family's current heads over.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["SET_MTP_HEADS"]

SET_MTP_HEADS: ToolSpec = ToolSpec(
    name="set_mtp_heads",
    external_name="Set MTP Heads",
    user_description="Add MTP draft heads to a model family",
    description=(
        "Add standalone MTP (multi-token prediction) draft heads to a local-LLM "
        "catalog family. Every quant of the family can then pick any of them for "
        "speculative decoding. The family's current heads are kept; a head whose id "
        "is already listed for the same file is left unchanged, and nothing is "
        "written (`path` is empty) when every head was already listed.\n\n"
        "The tool reads each head's GGUF header and refuses a file that carries no "
        "MTP layers, a head that borrows the target model's tensors "
        "(`shared_target_tensors`, usually named `…-shared-…`: mainline llama.cpp "
        "cannot load those), a head whose architecture differs from the family's "
        "quants, an id already used for a different file, and a family with no "
        "entries yet. It derives each head's `size_hint` and sets the minimum "
        "llama.cpp build that loads standalone heads.\n\n"
        "When to use: once, after the family's quants are added, when "
        "`read_hf_model` listed `mtp_head_files` — pass every self-contained head."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "base_llm": {
                "type": "string",
                "description": "The family the heads belong to — the `base_llm` its quants use.",
            },
            "heads": {
                "type": "array",
                "description": "The heads to add.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Lowercase slug, e.g. `q4_0`, `bf16`. Never "
                            "`off` or `builtin`.",
                        },
                        "repo_id": {"type": "string"},
                        "filename": {
                            "type": "string",
                            "description": "The head's path in the repo.",
                        },
                        "quant_type": {"type": "string", "description": "e.g. `Q8_0`."},
                    },
                    "required": ["id", "repo_id", "filename", "quant_type"],
                },
            },
        },
        "required": ["base_llm", "heads"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file written; empty when nothing changed.",
            },
            "heads": {"type": "array", "description": "Every head the family now offers."},
            "added": {"type": "array", "description": "Ids added by this call."},
            "picker_knobs": {"type": "array", "description": "The head-picker knob ids."},
        },
        "required": ["path", "heads", "added"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"base_llm": "always", "heads": "visible"},
    output_visibility={
        "path": "always",
        "heads": "visible",
        "added": "visible",
        "picker_knobs": "hidden",
    },
)
