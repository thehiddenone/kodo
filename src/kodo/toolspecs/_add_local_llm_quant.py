"""``add_local_llm_quant`` tool spec — write one quant into the user's local-LLM catalog.

Backs the Model Importer (doc/LLM_REGISTRY.md §4.0b). The tool writes
``~/.kodo/local_llms/<base_llm>/<name>.json``; the registry re-reads that
directory on every call, so the entry is offered without a restart.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["ADD_LOCAL_LLM_QUANT"]

_STR = {"type": "string"}

ADD_LOCAL_LLM_QUANT: ToolSpec = ToolSpec(
    name="add_local_llm_quant",
    external_name="Add Local LLM Quant",
    user_description="Add a quant to the local LLM catalog",
    description=(
        "Add one GGUF quant to the user's local-LLM catalog as "
        "`<user catalog>/<base_llm>/<name>.json`. One call per quant.\n\n"
        "The tool derives — and you do not pass — `size_hint` (the quant's summed "
        "shard sizes), `context_window` (the GGUF header's `<arch>.context_length`), "
        "the six shared knobs, and an F16 KV-cache default for 16-bit quants. It "
        "checks two of your claims against the GGUF header and refuses a mismatch: "
        "`builtin_mtp` must equal `nextn_predict_layers > 0` (true adds the "
        "built-in MTP speculative-decoding knob), and `context_knob` must be the "
        "context knob whose architecture equals the GGUF's `general.architecture` — "
        "required when one exists (see `list_local_llms`' `context_knobs`), empty "
        "when none does. It also refuses a `name` any served entry already uses, a "
        "`repo_id`+`filename` another entry already serves, a `filename` that is "
        "not the first file of a quant in the repo, and a `quant_type` with no "
        "readable bit width. Every refusal is an `error` naming the cause and, where "
        "there is one, the value that would be accepted.\n\n"
        "When to use: once per chosen quant, after `read_gguf_header` on it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "base_llm": {
                **_STR,
                "description": "Family directory, e.g. `Qwen38-27B`. Reuse an existing "
                "family's exact value when the model is the same model at the same size.",
            },
            "name": {
                **_STR,
                "description": "Entry name: lowercase letters, digits, `.` and `-`, "
                "e.g. `unsloth-qwen38-27b-ud-q4-k-xl`.",
            },
            "repo_id": {**_STR, "description": "The GGUF repository id."},
            "filename": {
                **_STR,
                "description": "The quant's file path in the repo — the first shard of "
                "a split GGUF.",
            },
            "quant_type": {**_STR, "description": "As the quantizer spells it, e.g. `UD-Q4_K_XL`."},
            "description": {
                **_STR,
                "description": "One line: `<Model name> <variant> <quant_type> by "
                "<quant_author>`, `<variant>` (e.g. `abliterated`) left out for a plain "
                "requant.",
            },
            "quant_author": {
                **_STR,
                "description": "The GGUF repository's owner — the account the files come "
                "from (e.g. `huihui-ai`), not the header's `general.quantized_by`.",
            },
            "llm_author": {**_STR, "description": "Who made the model, e.g. `Alibaba Cloud`."},
            "license_name": {**_STR, "description": "License display name."},
            "license_url": {**_STR, "description": "License URL, or empty."},
            "gpu_tip": {**_STR, "description": "One or two sentences on running it with a GPU."},
            "mac_tip": {**_STR, "description": "One sentence on which Mac memory size fits it."},
            "llamacpp_version": {
                "type": "integer",
                "description": "Minimum llama.cpp build number (`bNNNN` without the `b`); "
                "0 when unknown.",
            },
            "min_memory": {"type": "integer", "description": "Minimum memory tier in GB."},
            "memory": {
                "type": "integer",
                "description": "Recommended memory tier in GB; at least `min_memory`.",
            },
            "builtin_mtp": {
                "type": "boolean",
                "description": "Whether the GGUF header's `nextn_predict_layers` is above 0.",
            },
            "context_knob": {
                **_STR,
                "description": "The context knob id for the GGUF's architecture, or empty "
                "when no knob targets it.",
            },
        },
        "required": [
            "base_llm",
            "name",
            "repo_id",
            "filename",
            "quant_type",
            "description",
            "quant_author",
            "llm_author",
            "license_name",
            "license_url",
            "gpu_tip",
            "mac_tip",
            "llamacpp_version",
            "min_memory",
            "memory",
            "builtin_mtp",
            "context_knob",
        ],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The catalog file written."},
            "name": {"type": "string"},
            "base_llm": {"type": "string"},
            "size_hint": {"type": "string"},
            "context_window": {"type": "integer"},
            "mtp_supported": {"type": "boolean"},
            "knobs": {"type": "array"},
            "knob_defaults": {"type": "object"},
            "thinking_family": {
                "type": ["string", "null"],
                "description": "The family's reasoning-tier mechanism; null when it has none.",
            },
            "family_mtp_heads": {"type": "array"},
        },
        "required": ["path", "name", "base_llm", "mtp_supported", "knobs"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "base_llm": "always",
        "name": "always",
        "repo_id": "visible",
        "filename": "visible",
        "quant_type": "visible",
        "description": "hidden",
        "quant_author": "hidden",
        "llm_author": "hidden",
        "license_name": "hidden",
        "license_url": "hidden",
        "gpu_tip": "hidden",
        "mac_tip": "hidden",
        "llamacpp_version": "visible",
        "min_memory": "visible",
        "memory": "visible",
        "builtin_mtp": "visible",
        "context_knob": "visible",
    },
    output_visibility={
        "path": "always",
        "name": "hidden",
        "base_llm": "hidden",
        "size_hint": "visible",
        "context_window": "visible",
        "mtp_supported": "visible",
        "knobs": "hidden",
        "knob_defaults": "hidden",
        "thinking_family": "visible",
        "family_mtp_heads": "hidden",
    },
)
