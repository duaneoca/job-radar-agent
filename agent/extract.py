"""
Deterministic link extraction — the V2 pipeline's first stage.

Instead of sending the model an entire email (30–150 KB of HTML), we pull every usable link out of
the email ourselves and give the model a short numbered list. The model only answers with numbers
(plus the title/company it read), so it can never invent or mangle a URL — the URL always comes from
this module, never from the model.

Each candidate carries:
  • `text`    — the link's visible text (usually the job title)
  • `url`     — the link target, already scheme-allowlisted by `clean_link` (http/https only) [C2]
  • `context` — visible text immediately around the link, so the model can find the company name
                (LinkedIn puts it on the next line; Glassdoor puts it inside the link text)

We NEVER fetch or follow any URL (M2) — this is string processing only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .links import clean_link

MAX_LINKS = 150          # bound the prompt size even for pathological emails
CONTEXT_CHARS = 120      # visible characters kept on each side of a link

# Zero-width / invisible junk (LinkedIn floods alerts with U+034F), plus non-breaking space.
_INVISIBLE = re.compile("[\u00ad\u034f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_WS = re.compile(r"\s+")

_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "table", "ul", "ol", "section", "article",
    "header", "footer", "center", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP_TAGS = {"script", "style", "head", "title", "noscript"}

# Pure boilerplate link texts — never job postings. Kept deliberately small: anything ambiguous
# (e.g. "View job") stays in the list and the model decides.
_BOILERPLATE = {
    "unsubscribe", "privacy policy", "privacy", "terms", "terms of service", "terms of use",
    "manage settings", "manage alerts", "email preferences", "help center", "help", "log in",
    "sign in", "edit profile", "pause these emails", "view in browser", "view this email in your browser",
}


def norm(s: str) -> str:
    """Visible-text normalization: drop invisible chars, collapse whitespace."""
    s = _INVISIBLE.sub("", s or "").replace("\xa0", " ")
    return _WS.sub(" ", s).strip()


def match_key(s: str) -> str:
    """Comparison form used by the verifier: NFKC + normalized whitespace + casefold."""
    return norm(unicodedata.normalize("NFKC", s or "")).casefold()


@dataclass(frozen=True)
class LinkCandidate:
    id: int
    text: str
    url: str
    context: str

    def display_url(self, limit: int = 60) -> str:
        """host + path only (no query string) — a useful hint for the model, without tracking junk."""
        parts = urlsplit(self.url)
        shown = f"{parts.netloc}{parts.path}"
        return shown if len(shown) <= limit else shown[: limit - 1] + "…"


class _Collector(HTMLParser):
    """Flattens HTML to visible text while recording where each <a href> starts and ends."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.skip = 0
        self.anchors: list[tuple[str, int, int]] = []
        self._open: tuple[str, int] | None = None

    def _emit(self, s: str) -> None:
        self.parts.append(s)
        self.length += len(s)

    def _close_anchor(self) -> None:
        if self._open is not None:
            href, start = self._open
            self.anchors.append((href, start, self.length))
            self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip = 0          # a missing </head> must not hide the whole body
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._emit("\n")
        if tag == "a":
            self._close_anchor()   # unclosed <a> — close it before opening the next
            href = dict(attrs).get("href")
            if href:
                self._open = (href, self.length)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "a":
            self._close_anchor()
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if not self.skip:
            self._emit(data)

    def close(self):
        super().close()
        self._close_anchor()


_JOINED_GAP = 20   # a gap this short (e.g. " at ") means the next link belongs to this posting


def _from_html(html: str, chars: int) -> list[tuple[str, str, str]]:
    """(href, link text, context) per anchor.

    Context is bounded by the NEIGHBORING links so one posting's company can't be credited to another:
      • before — only text on the same line as the link, since the previous link
      • after  — text up to the next link; if that gap is tiny (Lenny's "Title at <a>Company</a>"),
                 the next link's text is included too
    """
    p = _Collector()
    try:
        p.feed(html)
        p.close()
    except Exception:          # malformed HTML — keep whatever was collected
        pass
    doc = "".join(p.parts)
    anchors = p.anchors
    out = []
    for i, (href, s, e) in enumerate(anchors):
        prev_end = anchors[i - 1][2] if i else 0
        next_start = anchors[i + 1][1] if i + 1 < len(anchors) else len(doc)
        before = norm(doc[max(prev_end, s - chars * 4):s].rsplit("\n", 1)[-1])[-chars:]
        after = norm(doc[e:min(next_start, e + chars * 4)])[:chars]
        if i + 1 < len(anchors) and len(after) <= _JOINED_GAP:
            _, ns, ne = anchors[i + 1]
            after = norm(f"{after} {doc[ns:ne]}")[:chars]
        text = norm(doc[s:e])
        out.append((href, text, norm(f"{before} {text} {after}")))
    return out


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")


def _from_text(text: str, chars: int) -> list[tuple[str, str, str]]:
    """Plain-text email fallback: the link text is the rest of the URL's line."""
    out = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        for m in _URL_IN_TEXT.finditer(line):
            label = norm(line.replace(m.group(0), " "))
            around = norm(" ".join(lines[max(0, i - 1): i + 2]))
            out.append((m.group(0), label, around[: chars * 2]))
    return out


def extract_links(html: str, text: str = "", *, max_links: int = MAX_LINKS,
                  context_chars: int = CONTEXT_CHARS) -> list[LinkCandidate]:
    """Numbered, de-duplicated, scheme-safe link candidates in document order."""
    raw = _from_html(html, context_chars) if html else _from_text(text, context_chars)
    seen: set[tuple[str, str]] = set()
    out: list[LinkCandidate] = []
    for href, label, context in raw:
        url = clean_link(href)
        if not url or not label or label.casefold() in _BOILERPLATE:
            continue
        if (label, url) in seen:
            continue
        seen.add((label, url))
        out.append(LinkCandidate(id=len(out) + 1, text=label[:300], url=url, context=context))
        if len(out) >= max_links:
            break
    return out


def _safe(s: str) -> str:
    """Strip angle brackets so email text can't close the <links> delimiter (prompt injection)."""
    return s.replace("<", "").replace(">", "")


def render_candidates(cands: list[LinkCandidate]) -> str:
    """The DATA block the model sees: one numbered entry per link."""
    lines = []
    for c in cands:
        lines.append(f"[{c.id}] {_safe(c.text)}")
        lines.append(f"    url: {_safe(c.display_url())}")
        lines.append(f"    nearby text: {_safe(c.context)}")
    return "\n".join(lines)
