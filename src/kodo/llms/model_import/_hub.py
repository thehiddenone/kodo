"""What the importer reads from Hugging Face: a repo's card and files, and GGUF headers.

:class:`HubClient` is the seam between the import logic and the network —
:class:`HuggingFaceHub` is the real one, tests pass a fake. Both answer in
plain dataclasses, never ``huggingface_hub`` objects, so nothing past this
module depends on that library's shapes.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Protocol

import aiohttp
import certifi
import huggingface_hub
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

from kodo.llms.local import GgufMetadata, LocalModelError, read_gguf_metadata

from ._errors import ModelImportError

__all__ = ["HubClient", "HuggingFaceHub", "RepoFile", "RepoSnapshot"]

#: Longest model card returned; a card past it is cut and flagged.
_README_MAX_BYTES = 40_000
_README_TIMEOUT = aiohttp.ClientTimeout(total=30)
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


@dataclass(frozen=True)
class RepoFile:
    """One file in a repository.

    Attributes:
        path: Repo-relative path (``"MTP/mtp-Qwen3.8-27B-Q4_0.gguf"``).
        size: Size in bytes, ``0`` if the Hub did not report one.
    """

    path: str
    size: int


@dataclass(frozen=True)
class RepoSnapshot:
    """A repository's metadata, model card and file list.

    Attributes:
        repo_id: The repository id as requested.
        author: The owning user or organisation.
        gated: ``""`` for an open repo, else the Hub's gating mode
            (``"auto"``/``"manual"``).
        license: The card's ``license`` (an SPDX-ish id, or ``"other"``).
        license_name: The card's ``license_name`` (set when ``license`` is ``"other"``).
        license_link: The card's ``license_link``, made absolute when the card
            gives a repo-relative path.
        base_models: The card's ``base_model`` ids.
        tags: The repo's tags.
        pipeline_tag: The Hub's task tag (``"text-generation"``).
        readme: The model card text, ``""`` when the repo has none.
        readme_truncated: Whether *readme* was cut short.
        files: Every file in the repo.
    """

    repo_id: str
    author: str
    gated: str
    license: str
    license_name: str
    license_link: str
    base_models: tuple[str, ...]
    tags: tuple[str, ...]
    pipeline_tag: str
    readme: str
    readme_truncated: bool
    files: tuple[RepoFile, ...]


class HubClient(Protocol):
    """Where repository snapshots and GGUF headers come from."""

    async def snapshot(self, repo_id: str) -> RepoSnapshot:
        """Fetch one repository's metadata, card and file list.

        Args:
            repo_id (str): Hugging Face repository id.

        Returns:
            RepoSnapshot: The repository.

        Raises:
            ModelImportError: The repo does not exist, is gated, or the Hub failed.
        """
        ...

    async def gguf_header(self, repo_id: str, filename: str) -> GgufMetadata:
        """Read one GGUF file's header.

        Args:
            repo_id (str): Hugging Face repository id.
            filename (str): The ``.gguf`` file — the first shard of a split one.

        Returns:
            GgufMetadata: The parsed header.

        Raises:
            ModelImportError: The file is missing, gated, or not a readable GGUF.
        """
        ...


def _card_str(card: object, key: str) -> str:
    getter = getattr(card, "get", None)
    if getter is None:
        return ""
    value = getter(key)
    return value if isinstance(value, str) else ""


def _card_list(card: object, key: str) -> tuple[str, ...]:
    getter = getattr(card, "get", None)
    if getter is None:
        return ()
    value = getter(key)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


class HuggingFaceHub:
    """:class:`HubClient` over the public Hugging Face Hub.

    Reads anonymously unless ``huggingface_hub`` finds a token of its own
    (``HF_TOKEN`` or a ``huggingface-cli login``), which is then used for the
    card, the file list and the GGUF range requests alike.
    """

    async def snapshot(self, repo_id: str) -> RepoSnapshot:
        """Fetch one repository's metadata, card and file list.

        Args:
            repo_id (str): Hugging Face repository id.

        Returns:
            RepoSnapshot: The repository.

        Raises:
            ModelImportError: The repo does not exist, is gated, or the Hub failed.
        """
        token = huggingface_hub.get_token()
        try:
            info = await asyncio.to_thread(
                huggingface_hub.model_info, repo_id, files_metadata=True, token=token
            )
        except GatedRepoError as exc:
            raise ModelImportError(
                f"{repo_id} is a gated repository and no Hugging Face token grants access to it"
            ) from exc
        except RepositoryNotFoundError as exc:
            raise ModelImportError(f"Hugging Face repository not found: {repo_id}") from exc
        except HfHubHTTPError as exc:
            raise ModelImportError(f"Hugging Face request for {repo_id} failed: {exc}") from exc
        card = info.card_data
        link = _card_str(card, "license_link")
        if link and "://" not in link:
            link = f"https://huggingface.co/{repo_id}/blob/main/{link.lstrip('./')}"
        readme, truncated = await self.__readme(repo_id, token)
        gated = info.gated
        return RepoSnapshot(
            repo_id=repo_id,
            author=info.author or "",
            gated=gated if isinstance(gated, str) else "",
            license=_card_str(card, "license"),
            license_name=_card_str(card, "license_name"),
            license_link=link,
            base_models=_card_list(card, "base_model"),
            tags=tuple(info.tags or ()),
            pipeline_tag=info.pipeline_tag or "",
            readme=readme,
            readme_truncated=truncated,
            files=tuple(
                RepoFile(path=sibling.rfilename, size=sibling.size or 0)
                for sibling in (info.siblings or [])
            ),
        )

    async def gguf_header(self, repo_id: str, filename: str) -> GgufMetadata:
        """Read one GGUF file's header.

        Args:
            repo_id (str): Hugging Face repository id.
            filename (str): The ``.gguf`` file — the first shard of a split one.

        Returns:
            GgufMetadata: The parsed header.

        Raises:
            ModelImportError: The file is missing, gated, or not a readable GGUF.
        """
        try:
            return await read_gguf_metadata(repo_id, filename, token=huggingface_hub.get_token())
        except LocalModelError as exc:
            raise ModelImportError(f"{repo_id}/{filename}: {exc}") from exc

    @staticmethod
    async def __readme(repo_id: str, token: str | None) -> tuple[str, bool]:
        url = huggingface_hub.hf_hub_url(repo_id, "README.md")
        headers = {"authorization": f"Bearer {token}"} if token else {}
        try:
            async with (
                aiohttp.ClientSession(timeout=_README_TIMEOUT) as http,
                http.get(url, headers=headers, ssl=_SSL_CONTEXT) as resp,
            ):
                if resp.status != 200:
                    return "", False
                data = bytearray()
                while len(data) <= _README_MAX_BYTES:
                    part = await resp.content.read(_README_MAX_BYTES + 1 - len(data))
                    if not part:
                        break
                    data += part
        except aiohttp.ClientError:
            return "", False
        truncated = len(data) > _README_MAX_BYTES
        return bytes(data[:_README_MAX_BYTES]).decode("utf-8", errors="replace"), truncated
