"""Static HTML extraction for the ``curl`` backend (doc/READ_WEBPAGE.md).

When ``read_webpage``/``query_search_engine`` fetch via ``curl_cffi``
(:mod:`kodo.websearch._curlfetch`) there is no live browser DOM to evaluate
JS in, so this module is a **from-scratch Python port** of what the browser
paths do in-page (``_readpage.py``'s ``_EXTRACT_MARKDOWN_JS`` and
``_engines.py``'s per-engine wall/extract JS), using :mod:`selectolax` (a
fast CSS-selector-capable HTML parser) instead of a live DOM.

Deliberately a separate module from the JS paths, not a shared abstraction —
same "duplicate so one path can't regress the other" philosophy the codebase
already uses between ``_scrape.py``/``_readpage.py``. One real difference
from the live-DOM walker: there is no CSS engine here, so elements hidden via
``display:none``/``visibility:hidden`` are not detected (only structurally
removed/``aria-hidden`` elements are) — an accepted gap for this best-effort,
static-parse path.
"""

from __future__ import annotations

from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

__all__ = ["extract_html", "extract_off", "extract_text", "is_blocked"]

# Elements stripped before `content_filter: "text"` extraction — mirrors
# _readpage.py's _REMOVE_SELECTORS (minus media, which read_webpage's text
# mode also drops; kept here for parity since this module has no separate
# scrape-only variant).
_REMOVE_SELECTORS = (
    "script, style, noscript, template, svg, canvas, iframe, "
    "nav, header, footer, aside, form, button, select, dialog, "
    "img, picture, video, audio, source, track, "
    '[role="navigation"], [role="banner"], [role="contentinfo"], '
    '[role="complementary"], [role="search"], [aria-hidden="true"]'
)

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
        "blockquote",
        "pre",
        "hr",
        "li",
    }
)

# Generic anti-bot / captcha wall detector — mirrors _readpage.py's _BLOCKED_JS.
_WALL_SELECTOR = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], #cf-challenge-running, '
    ".cf-browser-verification, #challenge-running, #challenge-form, .px-captcha-container"
)
_WALL_PATTERNS = (
    "verify you are human",
    "verify that you are a human",
    "checking your browser before accessing",
    "unusual traffic from your computer",
    "are you a robot",
    "bot detection",
    "just a moment...",
    "ddos protection by",
    "attention required! | cloudflare",
    "access denied",
    "please enable javascript and cookies",
    "to continue, please verify",
)


def is_blocked(html: str) -> bool:
    """``True`` when *html* looks like an anti-bot/captcha wall."""
    tree = HTMLParser(html)
    if tree.css_first(_WALL_SELECTOR) is not None:
        return True
    title_node = tree.css_first("title")
    title_text = title_node.text(strip=True) if title_node else ""
    body = tree.body
    body_text = body.text(separator=" ", strip=True)[:1000] if body is not None else ""
    haystack = f"{title_text} {body_text}".lower()
    return any(pattern in haystack for pattern in _WALL_PATTERNS)


def extract_off(html: str) -> str:
    """``content_filter: "off"`` — the page source, completely untouched."""
    return html


def extract_html(html: str) -> str:
    """``content_filter: "html"`` — full-page HTML, script/style/noscript removed."""
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript"):
        node.decompose()
    return tree.html or ""


def extract_text(html: str, base_url: str) -> tuple[str, str]:
    """``content_filter: "text"`` — content-root select + HTML→Markdown walk.

    Args:
        html: The fetched page's raw HTML.
        base_url: The page's URL, used to resolve relative ``<a href>``s to
            absolute links (mirrors the browser path's ``document.baseURI``).

    Returns:
        tuple[str, str]: ``(title, markdown)``.
    """
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    for node in tree.css(_REMOVE_SELECTORS):
        node.decompose()
    root = (
        tree.css_first("article")
        or tree.css_first("main")
        or tree.css_first('[role="main"]')
        or tree.body
    )
    markdown = _MarkdownWalker(base_url).walk(root) if root is not None else ""
    return title, markdown


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def _direct_children(node: Node) -> list[Node]:
    return list(node.iter(include_text=False))


def _table_rows(table: Node) -> list[Node]:
    """Direct ``<tr>`` rows, recursing one level into thead/tbody/tfoot."""
    rows: list[Node] = []
    for child in _direct_children(table):
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(c for c in _direct_children(child) if c.tag == "tr")
    return rows


def _row_cells(row: Node) -> list[Node]:
    return [c for c in _direct_children(row) if c.tag in ("th", "td")]


class _MarkdownWalker:
    """Converts a content-root subtree to Markdown, base-URL-aware for links.

    Args:
        base_url: The page's URL, used to resolve relative ``<a href>``s to
            absolute links.
    """

    __base_url: str

    def __init__(self, base_url: str) -> None:
        self.__base_url = base_url

    def walk(self, node: Node) -> str:
        """Render *node*'s block-level children as Markdown, joined by blank lines.

        Args:
            node: The content-root subtree to convert (its children are
                walked; ``node`` itself is not wrapped in any markup).

        Returns:
            str: The rendered Markdown.
        """
        parts: list[str] = []
        buffer = ""

        def flush() -> None:
            nonlocal buffer
            text = _norm_ws(buffer)
            if text:
                parts.append(text)
            buffer = ""

        for child in node.iter(include_text=True):
            if child.tag == "-text":
                buffer += child.text(deep=False) or ""
                continue
            if child.tag in _BLOCK_TAGS:
                flush()
                md = self.__block_to_markdown(child)
                if md:
                    parts.append(md)
            else:
                buffer += self.__inline_node(child)
        flush()
        return "\n\n".join(parts)

    def __inline_node(self, node: Node) -> str:
        """Render one non-block *node* (and its subtree) as inline text/markdown.

        Handles ``node`` itself being ``<a>``/``<br>`` — not just an ``<a>``
        nested a level deeper inside a wrapper — so a link with no wrapping
        inline element (e.g. ``<p>Hello <a href="/x">link</a></p>``, the
        common case) still becomes ``[link](...)`` instead of losing its href.
        """
        if node.tag == "br":
            return " "
        if node.tag == "a":
            href = node.attributes.get("href")
            text = _norm_ws(self.__inline(node))
            if href and text and not href.startswith("javascript:") and not href.startswith("#"):
                return f"[{text}]({urljoin(self.__base_url, href)})"
            return text
        return self.__inline(node)

    def __inline(self, node: Node) -> str:
        """Render *node*'s children as inline text/markdown (node is a container)."""
        parts: list[str] = []
        for child in node.iter(include_text=True):
            if child.tag == "-text":
                parts.append(child.text(deep=False) or "")
            else:
                parts.append(self.__inline_node(child))
        return "".join(parts)

    def __cell_text(self, cell: Node) -> str:
        return _norm_ws(self.__inline(cell)).replace("|", "\\|")

    def __table_to_markdown(self, table: Node) -> str:
        rows = _table_rows(table)
        if not rows:
            return ""
        grid = [[self.__cell_text(c) for c in _row_cells(r)] for r in rows]
        cols = max((len(row) for row in grid), default=0)
        if not cols:
            return ""

        def pad(row: list[str]) -> list[str]:
            return row + [""] * (cols - len(row))

        lines = ["| " + " | ".join(pad(grid[0])) + " |", "| " + " | ".join(["---"] * cols) + " |"]
        lines.extend("| " + " | ".join(pad(row)) + " |" for row in grid[1:])
        return "\n".join(lines)

    def __list_to_markdown(self, list_node: Node, depth: int) -> str:
        indent = "  " * depth
        entries: list[str] = []
        has_blocks = False
        for li in _direct_children(list_node):
            if li.tag != "li":
                continue
            inline_text = ""
            blocks: list[str] = []
            for child in li.iter(include_text=True):
                if child.tag == "-text":
                    inline_text += child.text(deep=False) or ""
                    continue
                tag = child.tag
                if tag in ("ul", "ol"):
                    nested = self.__list_to_markdown(child, depth + 1)
                    if nested:
                        blocks.append(nested)
                elif tag in ("pre", "table", "blockquote"):
                    md = self.__block_to_markdown(child)
                    if md:
                        blocks.append(md)
                elif child.css_first("pre, table, blockquote, ul, ol") is not None:
                    md = self.walk(child)
                    if md:
                        blocks.append(md)
                else:
                    inline_text += self.__inline_node(child)
            inline_text = _norm_ws(inline_text)
            if not inline_text and not blocks:
                continue
            entry = f"{indent}- {inline_text}" if inline_text else f"{indent}-"
            if blocks:
                has_blocks = True
                entry += "\n\n" + "\n\n".join(blocks)
            entries.append(entry)
        return ("\n\n" if has_blocks else "\n").join(entries)

    def __block_to_markdown(self, el: Node) -> str:
        tag = el.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            text = _norm_ws(self.__inline(el))
            return f"{'#' * level} {text}" if text else ""
        if tag == "table":
            return self.__table_to_markdown(el)
        if tag in ("ul", "ol"):
            return self.__list_to_markdown(el, 0)
        if tag == "pre":
            text = (el.text(deep=True) or "").strip()
            return f"```\n{text}\n```" if text else ""
        if tag == "blockquote":
            text = _norm_ws(self.__inline(el))
            return f"> {text}" if text else ""
        if tag == "hr":
            return "---"
        return self.walk(el)
