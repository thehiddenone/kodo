"""Which model a headless run uses: a local-registry entry or a cloud model.

``--model`` takes one of three spellings (doc/HEADLESS.md §2):

- ``ENTRY`` — a local-registry entry name (LLM + quant), e.g.
  ``unsloth-qwen36-27b-q4-k-xl``. Inference runs on a llama-server that the
  run spawns or attaches to by URL.
- ``local/ENTRY`` — the same, spelled explicitly. It is the only way to name a
  local entry whose own name contains a ``/``.
- ``VENDOR/MODEL_ID`` — a cloud model, e.g. ``anthropic/claude-sonnet-5`` or
  ``openrouter/qwen/qwen3-coder`` (only the first ``/`` splits). The model is
  pinned for every effort tier (``models.cloud_uniform.<vendor>``), so a
  sub-agent's declared ``capability`` cannot silently switch models mid-run.

Which vendors and model ids exist is the server's knowledge, not this
client's: an unknown one fails at the first LLM call as a runtime error.
``kodo-harbor`` validates both against the registry before queueing trials.
"""

from __future__ import annotations

import re

__all__ = ["LOCAL_MODEL_PREFIX", "ModelSpec"]

#: The explicit prefix for a local-registry entry (``local/ENTRY``).
LOCAL_MODEL_PREFIX = "local"

_VENDOR_RE = re.compile(r"[a-z][a-z0-9_-]*")


class ModelSpec:
    """A parsed ``--model`` value: a local entry or a cloud ``vendor/model_id``."""

    __vendor: str | None
    __name: str

    def __init__(self, name: str, vendor: str | None = None) -> None:
        """Bind the model identity.

        Args:
            name (str): The local-registry entry, or the cloud model id.
            vendor (str | None): The cloud vendor key; ``None`` for a local entry.

        Raises:
            ValueError: *name* is empty, or *vendor* is not a vendor-key shape.
        """
        if not name.strip():
            raise ValueError("The model name is empty")
        if vendor is not None and not _VENDOR_RE.fullmatch(vendor):
            raise ValueError(f"{vendor!r} is not a cloud vendor key")
        self.__name = name
        self.__vendor = vendor

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        """Parse a ``--model`` value.

        Args:
            text (str): ``ENTRY``, ``local/ENTRY`` or ``VENDOR/MODEL_ID``.

        Returns:
            ModelSpec: The parsed spec.

        Raises:
            ValueError: *text* is empty or malformed.
        """
        value = text.strip()
        if "/" not in value:
            return cls(value)
        prefix, _, rest = value.partition("/")
        if prefix == LOCAL_MODEL_PREFIX:
            return cls(rest)
        if not rest:
            raise ValueError(f"{text!r} names a vendor but no model id")
        return cls(rest, prefix.lower())

    @property
    def is_cloud(self) -> bool:
        """Whether this is a cloud model."""
        return self.__vendor is not None

    @property
    def vendor(self) -> str | None:
        """The cloud vendor key, or ``None`` for a local entry."""
        return self.__vendor

    @property
    def name(self) -> str:
        """The local-registry entry name, or the cloud model id."""
        return self.__name

    @property
    def label(self) -> str:
        """The canonical spelling: ``ENTRY`` or ``VENDOR/MODEL_ID``."""
        return self.__name if self.__vendor is None else f"{self.__vendor}/{self.__name}"

    def __repr__(self) -> str:
        return f"ModelSpec({self.label!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelSpec):
            return NotImplemented
        return (self.__vendor, self.__name) == (other.vendor, other.name)

    def __hash__(self) -> int:
        return hash((self.__vendor, self.__name))
