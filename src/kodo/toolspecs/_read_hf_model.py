"""``read_hf_model`` tool spec — one Hugging Face repo's card, license and GGUF files.

Backs the Model Importer (doc/LLM_REGISTRY.md §4.0b). Reads through the Hub API
rather than a web page, so the file list carries exact sizes and the card's
license fields come back structured.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_HF_MODEL"]

READ_HF_MODEL: ToolSpec = ToolSpec(
    name="read_hf_model",
    external_name="Read Hugging Face Model",
    user_description="Read a Hugging Face model repo",
    description=(
        "Read one Hugging Face model repository through the Hub API. Returns the "
        "card's metadata (`author`, `gated`, `license`, `license_name`, "
        "`license_link`, `base_models`, `tags`, `pipeline_tag`), the model card text "
        "(`readme`, cut at 40,000 bytes with `readme_truncated` set), and the repo's "
        "GGUF files sorted three ways: `gguf_quants` — one object per quant, a split "
        "GGUF collapsed to its first shard, with `filename`, `shards`, "
        "`total_bytes`, `size_hint`, a `quant_type_guess` read off the file name and "
        "its `precision_bits` (null when the name carries no quant type); "
        "`mtp_head_files` — standalone MTP draft heads (`MTP/` or `mtp-*.gguf`); and "
        "`mmproj_files` — vision projectors, which are never quants. "
        "`incomplete_split_files` lists split GGUFs with shards missing. Works on any "
        "model repo: on a base (safetensors) repo `gguf_quants` is simply empty. "
        "Reads anonymously unless an `HF_TOKEN` is set; a gated repo returns an "
        "`error`.\n\n"
        "When to use: to list the quants a GGUF repo offers, and to read the base "
        "model's card (its `base_models` entry) for its author, license and "
        "description."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo_id": {
                "type": "string",
                "description": "Repository id, `owner/name` (e.g. `unsloth/Qwen3.8-27B-GGUF`).",
            },
        },
        "required": ["repo_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "repo_id": {"type": "string"},
            "author": {"type": "string"},
            "gated": {"type": "string", "description": '"" when open, else the gating mode.'},
            "license": {"type": "string"},
            "license_name": {"type": "string"},
            "license_link": {"type": "string"},
            "base_models": {"type": "array"},
            "tags": {"type": "array"},
            "pipeline_tag": {"type": "string"},
            "gguf_quants": {"type": "array"},
            "mtp_head_files": {"type": "array"},
            "mmproj_files": {"type": "array"},
            "incomplete_split_files": {"type": "array"},
            "readme": {"type": "string"},
            "readme_truncated": {"type": "boolean"},
        },
        "required": ["repo_id", "gguf_quants", "mtp_head_files", "readme"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"repo_id": "always"},
    output_visibility={
        "repo_id": "hidden",
        "author": "visible",
        "gated": "hidden",
        "license": "visible",
        "license_name": "hidden",
        "license_link": "hidden",
        "base_models": "visible",
        "tags": "hidden",
        "pipeline_tag": "hidden",
        "gguf_quants": "visible",
        "mtp_head_files": "visible",
        "mmproj_files": "hidden",
        "incomplete_split_files": "hidden",
        "readme": "hidden",
        "readme_truncated": "hidden",
    },
)
