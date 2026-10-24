"""Shared HTML article extraction, originally tested by the TASS collector."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import trafilatura
from selectolax.parser import HTMLParser


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _publication_date(value: str | None) -> datetime | None:
    if not value or "T" not in value:
        return None  # Do not invent a publication time from a calendar date.
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


@dataclass
class Article:
    title: str
    text: str
    published_at: datetime | None


def extract_article(html: str, url: str, *, text_override: str | None = None) -> Article:
    tree = HTMLParser(html)
    metadata = {}
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            objects = _json_objects(json.loads(script.text()))
            for obj in objects:
                types = obj.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if "NewsArticle" in types or "Article" in types:
                    metadata = obj
                    break
        except (ValueError, TypeError):
            continue
        if metadata:
            break

    extracted = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_comments=False, include_tables=False, favor_precision=True,
    )
    data = json.loads(extracted) if extracted else {}
    text = (text_override if text_override is not None else data.get("text") or "").strip()
    title_node = tree.css_first('meta[property="og:title"]')
    h1 = tree.css_first("h1")
    title = (
        metadata.get("headline")
        or (title_node.attributes.get("content") if title_node else None)
        or (h1.text(strip=True) if h1 else None)
        or data.get("title") or ""
    ).strip()
    date_node = tree.css_first('meta[property="article:published_time"]')
    date = metadata.get("datePublished") or (
        date_node.attributes.get("content") if date_node else None
    )
    if not title or len(text) < 80 or title.lower() in ("forbidden", "access denied"):
        raise ValueError("Article title or full text was not found; HTML layout needs inspection")
    return Article(title, text, _publication_date(date))
