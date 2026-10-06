"""``read_gguf_header`` tool spec — one GGUF file's header metadata, without the weights.

Backs the Model Importer (doc/LLM_REGISTRY.md §4.0b). The header is the only
reliable source for whether a quant ships MTP layers and what its native
context and architecture are — model cards routinely omit both.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_GGUF_HEADER"]

READ_GGUF_HEADER: ToolSpec = ToolSpec(
    name="read_gguf_header",
    external_name="Read GGUF Header",
    user_description="Read a GGUF file's header",
    description=(
        "Read the key/value header of one `.gguf` file in a Hugging Face repo over "
        "HTTP range requests — typically 1-16 MB, never the weights. For a split GGUF "
        "pass the first shard (`…-00001-of-0000N.gguf`); it carries the metadata. "
        "Returns `architecture` (`general.architecture`), `context_length` "
        "(`<arch>.context_length`, the native context window), `nextn_predict_layers` "
        "(`<arch>.nextn_predict_layers`: greater than 0 means the file ships built-in "
        "MTP layers, 0 means the key is absent), `shared_target_tensors` (true for an "
        "MTP head that borrows the target model's tensors), `tensor_count`, "
        "`block_count`, `expert_count`, `expert_used_count`, `split_count`, "
        "`file_type`, `name`, `size_label`, `has_chat_template`, every other scalar "
        "key under `scalars` (strings over 2,000 characters cut), and every array key "
        "under `arrays` as its element type and length. Results are cached for the "
        "session.\n\n"
        "When to use: on every quant you intend to add, to learn whether it has "
        "built-in MTP and which context knob matches its architecture; and on every "
        "MTP head file, to tell self-contained heads from shared ones."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo_id": {
                "type": "string",
                "description": "Repository id, `owner/name`.",
            },
            "filename": {
                "type": "string",
                "description": "Path of the `.gguf` file within the repo.",
            },
        },
        "required": ["repo_id", "filename"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "version": {"type": "integer"},
            "tensor_count": {"type": "integer"},
            "architecture": {"type": "string"},
            "name": {"type": "string"},
            "size_label": {"type": "string"},
            "file_type": {"type": "integer"},
            "block_count": {"type": "integer"},
            "context_length": {"type": "integer"},
            "nextn_predict_layers": {"type": "integer"},
            "shared_target_tensors": {"type": "boolean"},
            "expert_count": {"type": "integer"},
            "expert_used_count": {"type": "integer"},
            "split_count": {"type": "integer"},
            "has_chat_template": {"type": "boolean"},
            "scalars": {"type": "object"},
            "arrays": {"type": "object"},
        },
        "required": ["architecture", "context_length", "nextn_predict_layers"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"repo_id": "visible", "filename": "always"},
    output_visibility={
        "version": "hidden",
        "tensor_count": "visible",
        "architecture": "visible",
        "name": "hidden",
        "size_label": "visible",
        "file_type": "hidden",
        "block_count": "hidden",
        "context_length": "visible",
        "nextn_predict_layers": "visible",
        "shared_target_tensors": "visible",
        "expert_count": "hidden",
        "expert_used_count": "hidden",
        "split_count": "hidden",
        "has_chat_template": "hidden",
        "scalars": "hidden",
        "arrays": "hidden",
    },
)
