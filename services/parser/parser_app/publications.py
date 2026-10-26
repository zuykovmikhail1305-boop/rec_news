"""Common URL -> HTML -> article pipeline. Does not import database code."""
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from .adapters import adapter_for, normalize_url
from .errors import CollectionError


def fetch(client, url):
    adapter = adapter_for(url)
    url = adapter.normalize(url)
    for attempt in range(6):
        try:
            response = client.get(url)
        except httpx.RequestError as exc:
            raise CollectionError(f"Ошибка загрузки: {type(exc).__name__}", retryable=True) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            try:
                url = adapter.normalize(urljoin(url, response.headers.get("location", "")))
            except ValueError as exc:
                raise CollectionError("Редирект за пределы публикаций выбранного источника") from exc
            time.sleep(5)
            continue
        if response.status_code != 200:
            retry_after = None
            value = response.headers.get("retry-after")
            if value:
                try:
                    retry_after = max(0, int(value))
                except ValueError:
                    try:
                        retry_after = max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            raise CollectionError(f"{adapter.name} вернул HTTP {response.status_code}",
                                  retryable=response.status_code == 429 or response.status_code >= 500,
                                  status_code=response.status_code, retry_after=retry_after)
        if "text/html" not in response.headers.get("content-type", "").lower():
            raise CollectionError("Ожидался HTML-документ")
        return response.text, url
    raise CollectionError("Слишком много перенаправлений")


def parse_publication(client, url):
    adapter = adapter_for(url)
    html, final_url = fetch(client, adapter.normalize(url))
    article = adapter.extract(html, final_url)
    return {"source": adapter.code, "url": final_url, **article,
            "parser_version": adapter.version, "raw_html": html}


def public_result(article):
    return {key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in article.items() if key != "raw_html"}
