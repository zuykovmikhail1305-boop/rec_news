"""Дешёвые отпечатки новости для дедупликации без эмбеддингов.

* :func:`normalize` — нормализация текста: нижний регистр, ``ё`` → ``е``,
  единые кавычки и тире, схлопнутые пробелы;
* :func:`text_hash` — SHA-1 нормализованного полного текста (точный дубль);
* :func:`normalize_url` — ключ URL без схемы, ``www.``, ``utm_*``-параметров,
  якоря и хвостового ``/``;
* :func:`text_minhash` — MinHash по шинглам из 4 слов для поиска почти
  полных копий через LSH (``datasketch``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import numpy as np
from datasketch import MinHash

NUM_PERM = 128
SHINGLE_SIZE = 4
MINHASH_SEED = 1
MINHASH_SCHEME = "affine32"

_QUOTES = str.maketrans({char: '"' for char in "«»„“”‟‹›'‘’‚`´"})
_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—―−"})
_SPACES_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"(?u)\w+")


def normalize(text: str) -> str:
    """Приводит текст к виду, в котором оформление не мешает сравнению."""
    text = text.lower().replace("ё", "е").translate(_QUOTES).translate(_DASHES)
    return _SPACES_RE.sub(" ", text).strip()


def text_hash(text: str) -> str:
    """SHA-1 нормализованного текста — ключ точного дубля."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def normalize_url(url: str | None) -> str | None:
    """Ключ URL: http и https, ``www.``, ``utm_*``, якорь и хвостовой ``/`` не различаются.

    Остальные параметры запроса сортируются. Для пустого URL возвращает ``None``.
    """
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip() if "//" in url else f"//{url.strip()}")
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    )
    key = host + parts.path.rstrip("/")
    return f"{key}?{urlencode(query)}" if query else key


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    """Шинглы из ``size`` подряд идущих слов нормализованного текста."""
    words = _WORD_RE.findall(normalize(text))
    if len(words) <= size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def text_minhash(text: str, num_perm: int = NUM_PERM) -> MinHash:
    """MinHash множества шинглов текста."""
    minhash = MinHash(num_perm=num_perm, seed=MINHASH_SEED, scheme=MINHASH_SCHEME)
    minhash.update_batch([shingle.encode("utf-8") for shingle in shingles(text)])
    return minhash


def minhash_to_bytes(minhash: MinHash) -> bytes:
    """Хеш-значения MinHash для хранения в базе."""
    return np.asarray(minhash.hashvalues, dtype=np.uint64).tobytes()


def minhash_from_bytes(data: bytes, num_perm: int = NUM_PERM) -> MinHash:
    """Восстанавливает MinHash из :func:`minhash_to_bytes`."""
    return MinHash(
        num_perm=num_perm,
        seed=MINHASH_SEED,
        scheme=MINHASH_SCHEME,
        hashvalues=np.frombuffer(data, dtype=np.uint64).copy(),
    )


@dataclass(frozen=True)
class Fingerprint:
    """Отпечатки одной новости: хеш текста, ключ URL и MinHash."""

    text_hash: str
    url_key: str | None
    minhash: MinHash

    @classmethod
    def of(cls, text: str, url: str | None) -> "Fingerprint":
        return cls(text_hash=text_hash(text), url_key=normalize_url(url), minhash=text_minhash(text))
