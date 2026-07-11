"""Filesystem-backed registry of reusable validation prompts.

The scenario suite reuses the same task / user-proxy / result-validation prompt
text across several LLMs-under-test. Rather than inline those strings in every
scenario ``.py`` file, they live here as ``.md`` files and scenarios pull them
by name::

    from kodo.validator.prompts import PROMPTS

    PROMPTS.get("tictactoe/detailed_task")   # → prompts/tictactoe/detailed_task.md

A name is a ``/``-separated path under the package root, with or without the
``.md`` suffix, so prompts group into sub-directories ("submodules") — e.g. one
folder per scenario family. Convention for a family: put its prompts in their
own sub-directory and suffix the files ``_task`` (the prompt under test),
``_upp`` (user-proxy prompt), and ``_rvp`` (result-validation prompt); share one
``_upp`` and one ``_rvp`` across variants that differ only in their ``_task``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PROMPTS", "PromptNotFoundError", "PromptRegistry"]


class PromptNotFoundError(KeyError):
    """A requested prompt name does not resolve to a ``.md`` file under the root."""


class PromptRegistry:
    """Loads prompt text from ``.md`` files under a package directory.

    Names are resolved lazily and cached. The registry is read-only — it never
    writes — so the shipped, package-relative ``.md`` files are the single
    source of truth for every prompt.

    Args:
        root: Directory prompt names resolve against. Defaults to the package
            directory this module lives in.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or Path(__file__).resolve().parent).resolve()
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        """Return the text of the prompt named *name*.

        Args:
            name: A ``/``-separated path under the registry root, with or
                without a trailing ``.md`` (e.g. ``tictactoe/rvp`` or
                ``tictactoe/rvp.md``).

        Returns:
            str: The file's UTF-8 text.

        Raises:
            PromptNotFoundError: If *name* is malformed (absolute, or escaping
                the root) or resolves to no ``.md`` file.
        """
        if name in self._cache:
            return self._cache[name]
        path = self._resolve(name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            available = ", ".join(self.names()) or "(none)"
            raise PromptNotFoundError(
                f"No prompt {name!r} (looked for "
                f"{path.relative_to(self._root)} under {self._root}). "
                f"Available: {available}"
            ) from exc
        self._cache[name] = text
        return text

    def names(self) -> list[str]:
        """Every available prompt name (its ``/``-joined path, no ``.md``), sorted."""
        return sorted(
            "/".join(p.relative_to(self._root).with_suffix("").parts)
            for p in self._root.rglob("*.md")
        )

    def _resolve(self, name: str) -> Path:
        """Map a prompt name to the ``.md`` path it names, guarding traversal.

        Args:
            name: The requested prompt name.

        Returns:
            Path: The resolved ``.md`` file path (not guaranteed to exist).

        Raises:
            PromptNotFoundError: If *name* is empty, absolute, or would escape
                the registry root.
        """
        stem = name[:-3] if name.endswith(".md") else name
        parts = [p for p in stem.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or Path(stem).is_absolute():
            raise PromptNotFoundError(f"Invalid prompt name: {name!r}")
        path = self._root.joinpath(*parts).with_suffix(".md").resolve()
        if not path.is_relative_to(self._root):
            raise PromptNotFoundError(f"Prompt name escapes the registry root: {name!r}")
        return path


# Package-wide singleton, rooted at this package's directory.
PROMPTS = PromptRegistry()
