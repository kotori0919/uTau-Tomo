from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


TRACKING_QUERY_KEYS = {"hl", "type"}


def canonicalize_url(url: str) -> str:
    """Return a stable URL suitable for deduplication."""
    parts = urlsplit(url.strip())
    path = parts.path
    if parts.hostname in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}:
        match = re.fullmatch(r"/(@[\w.]+)/post/([\w-]+)(?:/media)?/?", path)
        if match:
            handle, post_id = match.groups()
            return f"https://www.threads.com/{handle.lower()}/post/{post_id}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith("utm_")
    ]
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


@dataclass(slots=True)
class Article:
    source: str
    url: str
    title: str
    body: str
    published_at: str | None = None
    section: str = ""
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = canonicalize_url(self.url)
        self.title = " ".join(self.title.split())[:300]
        self.body = " ".join(self.body.split())[:2000]
        self.section = " ".join(self.section.split())[:120]

    @property
    def id(self) -> str:
        value = f"{self.source}\n{self.url}".encode()
        return sha256(value).hexdigest()

    @property
    def date_label(self) -> str:
        if not self.published_at:
            return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d")
        try:
            return datetime.fromisoformat(self.published_at).strftime("%Y.%m.%d")
        except ValueError:
            return self.published_at[:10].replace("-", ".")


@dataclass(slots=True)
class SourceBatch:
    source: str
    articles: list[Article]
    status: str = "ok"
    detail: str = ""


@dataclass(slots=True)
class KnowledgeDecision:
    keep: bool
    knowledge_score: float
    promotion_score: float
    knowledge_anchor: str
    promotion_anchor: str
    reason: str
