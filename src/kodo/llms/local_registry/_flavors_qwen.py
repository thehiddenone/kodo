"""Qwen-family long-context :class:`LlamaFlavor` builders.

YaRN rope-scaling extends a Qwen GGUF's native 256K context out to 512K or
1M — dense and MoE variants need different ``--override-kv`` metadata keys
(``qwen35.context_length`` vs. ``qwen35moe.context_length``), hence four
functions rather than one parameterized by architecture. ``platform=MAC``
on every one of them: the KV cache at these sizes is impractical to split
across a discrete GPU's VRAM and system RAM, so they're only offered on
Apple Silicon's unified-memory pool.
"""

from __future__ import annotations

from ._types import LlamaFlavor, LlamaFlavorPlatform


def make_qwen_512k_kv_q8(id: str, name: str) -> LlamaFlavor:
    return LlamaFlavor(
        id=id,
        name=name,
        platform=LlamaFlavorPlatform.MAC,
        description="Default flavor",
        llama_args={
            "--ctx-size": "524288",
            "--rope-scaling": "yarn",
            "--rope-scale": "2.0",
            "--yarn-orig-ctx": "262144",
            "--override-kv": "qwen35.context_length=int:524288",
            "--cache-type-k": "q8_0",
            "--cache-type-v": "q8_0",
            "--n-gpu-layers": "-1",
            "--reasoning-format": "auto",
            "--jinja": "",
        },
    )


def make_qwen_1m_kv_q8(id: str, name: str) -> LlamaFlavor:
    return LlamaFlavor(
        id=id,
        name=name,
        platform=LlamaFlavorPlatform.MAC,
        description="Default flavor",
        llama_args={
            "--ctx-size": "1048576",
            "--rope-scaling": "yarn",
            "--rope-scale": "4.0",
            "--yarn-orig-ctx": "262144",
            "--override-kv": "qwen35.context_length=int:1048576",
            "--cache-type-k": "q8_0",
            "--cache-type-v": "q8_0",
            "--n-gpu-layers": "-1",
            "--reasoning-format": "auto",
            "--jinja": "",
        },
    )


def make_qwen_moe_512k_kv_q8(id: str, name: str) -> LlamaFlavor:
    return LlamaFlavor(
        id=id,
        name=name,
        platform=LlamaFlavorPlatform.MAC,
        description="Default flavor",
        llama_args={
            "--ctx-size": "524288",
            "--rope-scaling": "yarn",
            "--rope-scale": "2.0",
            "--yarn-orig-ctx": "262144",
            "--override-kv": "qwen35moe.context_length=int:524288",
            "--cache-type-k": "q8_0",
            "--cache-type-v": "q8_0",
            "--n-gpu-layers": "-1",
            "--reasoning-format": "auto",
            "--jinja": "",
        },
    )


def make_qwen_moe_1m_kv_q8(id: str, name: str) -> LlamaFlavor:
    return LlamaFlavor(
        id=id,
        name=name,
        platform=LlamaFlavorPlatform.MAC,
        description="Default flavor",
        llama_args={
            "--ctx-size": "1048576",
            "--rope-scaling": "yarn",
            "--rope-scale": "4.0",
            "--yarn-orig-ctx": "262144",
            "--override-kv": "qwen35moe.context_length=int:1048576",
            "--cache-type-k": "q8_0",
            "--cache-type-v": "q8_0",
            "--n-gpu-layers": "-1",
            "--reasoning-format": "auto",
            "--jinja": "",
        },
    )
