"""Shared data models."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any
import hashlib

@dataclass
class Article:
    id: str
    title: str
    summary: str = ""
    link: str = ""
    source: str = ""
    cat: str = "LOCAL"
    lang: str = "en"
    published_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    score: int = 0
    reliability: int = 0
    confidence: str = "low"  # high|medium|low
    is_breaking: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(title: str, link: str = "", source: str = "") -> str:
        raw = f"{source}|{link}|{title}".strip().lower().encode("utf-8", "ignore")
        return hashlib.sha1(raw).hexdigest()[:24]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Article":
        aid = data.get("id") or cls.make_id(data.get("title", ""), data.get("link", ""), data.get("source", ""))
        return cls(
            id=aid,
            title=data.get("title", "").strip(),
            summary=data.get("summary", "").strip(),
            link=data.get("link", "").strip(),
            source=data.get("source", "").strip(),
            cat=data.get("cat", "LOCAL"),
            lang=data.get("lang", "en"),
            published_at=data.get("published_at"),
            fetched_at=data.get("fetched_at"),
            score=int(data.get("score") or 0),
            reliability=int(data.get("reliability") or 0),
            confidence=data.get("confidence", "low"),
            is_breaking=bool(data.get("is_breaking", False)),
            meta=data.get("meta") or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "source": self.source,
            "cat": self.cat,
            "lang": self.lang,
            "published_at": self.published_at.isoformat() if hasattr(self.published_at, "isoformat") else self.published_at,
            "fetched_at": self.fetched_at.isoformat() if hasattr(self.fetched_at, "isoformat") else self.fetched_at,
            "score": self.score,
            "reliability": self.reliability,
            "confidence": self.confidence,
            "is_breaking": self.is_breaking,
            "meta": self.meta,
        }
