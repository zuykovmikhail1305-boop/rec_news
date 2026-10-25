"""Source-specific URL validation and HTML extraction; no database or network."""
import re
import json
from datetime import date
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from selectolax.parser import HTMLParser

from .errors import CollectionError
from .extraction import extract_article, _json_objects

MONTHS = {name: i for i, name in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(), 1)}


def extract_cbr(html: str) -> dict:
    tree = HTMLParser(html)
    title_node = tree.css_first("h1")
    parts = []
    for section in tree.css(".lead-text, .landing-text"):
        for node in section.css("script, style, iframe, nav"):
            node.decompose()
        text = section.text(separator=" ", strip=True)
        if text:
            parts.append(re.sub(r"\s+", " ", text).strip())
    title = re.sub(r"\s+", " ", title_node.text(separator=" ", strip=True)).strip() if title_node else ""
    text = "\n\n".join(parts)
    if not title or len(text) < 80:
        raise CollectionError("Не найден заголовок или основной текст публикации; требуется проверка HTML")
    date_node = tree.css_first(".news-info-line_date")
    published = None
    if date_node:
        match = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", date_node.text().lower())
        if match and match[2] in MONTHS:
            try:
                published = date(int(match[3]), MONTHS[match[2]], int(match[1]))
            except ValueError:
                pass
    return {"title": title, "text": text, "published_date": published}


@dataclass(frozen=True)
class SourceAdapter:
    code: str
    name: str
    host: str
    listing_url: str
    version: str

    def normalize(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {self.host, "www." + self.host}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ValueError("Разрешены только HTTPS-публикации поддерживаемых источников")
        if self.code == "cbr":
            ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
            if (parsed.path not in {"/press/event/", "/press/pr/"} or len(ids) != 1
                    or not re.fullmatch(r"[1-9][0-9]*", ids[0])):
                raise ValueError("Передайте URL публикации ЦБ: /press/event/?id=... или /press/pr/?id=...")
            return "https://www.cbr.ru" + parsed.path + "?" + urlencode({"id": ids[0]})
        path = parsed.path.rstrip("/")
        if self.code == "ria":
            if not re.fullmatch(r"/[0-9]{8}/[a-z0-9-]+-[1-9][0-9]*\.html", path):
                raise ValueError("Передайте URL отдельной публикации РИА: /YYYYMMDD/slug-id.html")
            return "https://ria.ru" + path
        if self.code == "kommersant":
            if not re.fullmatch(r"/doc/[1-9][0-9]*", path):
                raise ValueError("Передайте URL публикации Коммерсанта: /doc/числовой-id")
            return "https://www.kommersant.ru" + path
        if self.code == "vedomosti":
            if not re.fullmatch(r"/[a-z0-9-]+/(?:news|articles)/[0-9]{4}/[0-9]{2}/[0-9]{2}/[0-9]+-[a-z0-9-]+", path):
                raise ValueError("Передайте URL отдельной новости или статьи Ведомостей")
            return "https://www.vedomosti.ru" + path
        if not re.fullmatch(r"/[a-z0-9-]+/[0-9]+", path):
            raise ValueError("Передайте URL отдельной публикации: /раздел/числовой-id")
        host = "www." + self.host if self.code == "interfax" else self.host
        return "https://" + host + path

    def extract(self, html, url):
        if self.code == "cbr":
            return {**extract_cbr(html), "published_at": None}
        try:
            if self.code == "kommersant":
                tree = HTMLParser(html)
                paragraphs = [re.sub(r"\s+", " ", n.text(separator=" ", strip=True)).strip()
                              for n in tree.css(".doc-body [class]")
                              if set(n.attributes.get("class", "").split()) & {"doc-text-block__paragraph", "doc-list-block__text"}]
                text = "\n\n".join(p for p in paragraphs if p)
                if len(text) < 80:
                    raise CollectionError("Не найден доступный основной текст публикации Коммерсанта")
                article = extract_article(html, url, text_override=text)
            elif self.code == "vedomosti":
                tree = HTMLParser(html)
                for script in tree.css('script[type="application/ld+json"]'):
                    try:
                        objects = list(_json_objects(json.loads(script.text())))
                    except (ValueError, TypeError):
                        continue
                    if any(obj.get("isAccessibleForFree") in (False, "false", "False") for obj in objects):
                        raise CollectionError("Публикация Ведомостей требует подписки; полный текст недоступен")
                paragraphs = [re.sub(r"\s+", " ", n.text(separator=" ", strip=True)).strip()
                              for n in tree.css(".box-paragraph__text")]
                text = "\n\n".join(p for p in paragraphs if p)
                if len(text) < 80:
                    raise CollectionError("Не найден доступный текст публикации Ведомостей")
                article = extract_article(html, url, text_override=text)
            else:
                article = extract_article(html, url)
        except (ValueError, TypeError) as exc:
            raise CollectionError(str(exc)) from exc
        return {"title": article.title, "text": article.text,
                "published_at": article.published_at,
                "published_date": article.published_at.date() if article.published_at else None}


SOURCES = (
    SourceAdapter("ria", "РИА Новости", "ria.ru", "https://ria.ru/export/rss2/index.xml", "ria-html-v1"),
    SourceAdapter("kommersant", "Коммерсантъ", "kommersant.ru", "https://www.kommersant.ru/rss/news.xml", "kommersant-html-v1"),
    SourceAdapter("vedomosti", "Ведомости", "vedomosti.ru", "https://www.vedomosti.ru/economics", "vedomosti-html-v1"),
    SourceAdapter("cbr", "Банк России", "cbr.ru", "https://www.cbr.ru/news/", "cbr-html-v1"),
    SourceAdapter("tass", "ТАСС", "tass.ru", "https://tass.ru/ekonomika", "tass-html-v1"),
    SourceAdapter("interfax", "Интерфакс", "interfax.ru", "https://www.interfax.ru/business/", "interfax-html-v1"),
)


def adapter_for(url):
    try:
        host = urlsplit(url).hostname
        for adapter in SOURCES:
            if host in {adapter.host, "www." + adapter.host}:
                adapter.normalize(url)
                return adapter
    except (TypeError, AttributeError) as exc:
        raise ValueError("Некорректный URL") from exc
    raise ValueError("Поддерживаются публикации Банка России, ТАСС, Интерфакса, Ведомостей, Коммерсанта и РИА Новости")


def normalize_url(url):
    return adapter_for(url).normalize(url)
