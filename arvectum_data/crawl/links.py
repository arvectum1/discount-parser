from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class ParsedAnchor:
    href: str
    text: str
    rel: tuple[str, ...]


class _AnchorParser(HTMLParser):
    def __init__(self, *, max_links: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_links = max_links
        self.anchors: list[ParsedAnchor] = []
        self.base_href: str | None = None
        self._current_href: str | None = None
        self._current_rel: tuple[str, ...] = ()
        self._current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        name = tag.casefold()
        values = {str(k).casefold(): ("" if v is None else str(v)) for k, v in attrs}
        if name == "base" and self.base_href is None:
            href = values.get("href", "").strip()
            if href:
                self.base_href = href
        if name != "a" or len(self.anchors) >= self.max_links:
            return
        href = values.get("href", "").strip()
        if not href:
            return
        self._current_href = href
        self._current_rel = tuple(
            token.casefold()
            for token in values.get("rel", "").split()
            if token.strip()
        )
        self._current_text = []

    def handle_data(self, data):
        if self._current_href is None:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            self._current_text.append(cleaned)

    def handle_endtag(self, tag):
        if tag.casefold() != "a" or self._current_href is None:
            return
        if len(self.anchors) < self.max_links:
            self.anchors.append(
                ParsedAnchor(
                    href=self._current_href,
                    text=" ".join(self._current_text).strip(),
                    rel=self._current_rel,
                )
            )
        self._current_href = None
        self._current_rel = ()
        self._current_text = []


def extract_anchors(html: str, *, max_links: int) -> tuple[str | None, tuple[ParsedAnchor, ...]]:
    parser = _AnchorParser(max_links=max_links)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.base_href, tuple(parser.anchors)


def canonicalize_url(base_url: str, href: str) -> str | None:
    candidate = href.strip()
    if not candidate or candidate.startswith("#"):
        return None
    try:
        absolute = urljoin(base_url, candidate)
        parsed = urlsplit(absolute)
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    host = hostname.casefold().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def origin_key(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    return scheme, host, port
