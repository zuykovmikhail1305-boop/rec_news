"""Дедупликация новостей на точном, текстовом и семантическом уровнях."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from datasketch import MinHash, MinHashLSH
from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class NewsDocument:
    """Новость, которую нужно проверить и добавить в индекс."""

    text: str
    url: str | None = None
    document_id: str | int | None = None
    publication_time: str | None = None
    event_type: str | None = None
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DuplicateMatch:
    """Информация о найденном документе-дубликате."""

    document_id: str | int
    level: str
    score: float
    url: str | None


class NewsDeduplicator:
    """Ищет точные, почти точные и семантические дубликаты новостей."""

    _TOKEN_RE = re.compile(r"(?u)\w+")

    def __init__(
        self,
        *,
        embedding_model: str = (
            "sentence-transformers/"
            "paraphrase-multilingual-MiniLM-L12-v2"
        ),
        shingle_size: int = 5,
        minhash_permutations: int = 128,
        near_duplicate_threshold: float = 0.8,
        semantic_duplicate_threshold: float = 0.88,
        lsh_threshold: float | None = None,
        database_path: str = "news.sqlite3",
    ) -> None:
        """Создаёт индексы дедупликации и загружает модель эмбеддингов."""
        if shingle_size < 1:
            raise ValueError("shingle_size должен быть больше нуля.")
        if not 0.0 < near_duplicate_threshold <= 1.0:
            raise ValueError("near_duplicate_threshold должен быть в диапазоне (0, 1].")
        if not 0.0 < semantic_duplicate_threshold <= 1.0:
            raise ValueError(
                "semantic_duplicate_threshold должен быть в диапазоне (0, 1]."
            )

        self.embedding_model_name = embedding_model
        self.shingle_size = shingle_size
        self.near_duplicate_threshold = near_duplicate_threshold
        self.semantic_duplicate_threshold = semantic_duplicate_threshold
        self._minhash_permutations = minhash_permutations
        self._model = SentenceTransformer(embedding_model)
        self._documents: dict[str, NewsDocument] = {}
        self._embeddings: dict[str, np.ndarray] = {}
        self._hash_to_id: dict[str, str] = {}
        self._url_to_id: dict[str, str] = {}
        self._minhashes: dict[str, MinHash] = {}
        self._database_path = database_path
        self._lsh = MinHashLSH(
            threshold=lsh_threshold or near_duplicate_threshold,
            num_perm=minhash_permutations,
        )
        self._create_database()

    def _create_database(self) -> None:
        """Создаёт таблицу уникальных новостей в SQLite-базе."""
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news (
                    document_id TEXT PRIMARY KEY,
                    full_text TEXT NOT NULL,
                    publication_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    @staticmethod
    def _normalise_text(text: str) -> str:
        """Приводит текст к нижнему регистру и убирает пунктуацию."""
        return " ".join(NewsDeduplicator._TOKEN_RE.findall(text.lower()))

    @staticmethod
    def _normalise_url(url: str | None) -> str | None:
        """Нормализует URL для сравнения точных ссылок."""
        if not url:
            return None
        return url.strip().lower().rstrip("/")

    @staticmethod
    def _text_hash(normalised_text: str) -> str:
        """Возвращает MD5-хэш нормализованного текста."""
        return hashlib.md5(
            normalised_text.encode("utf-8"), usedforsecurity=False
        ).hexdigest()

    def _shingles(self, normalised_text: str) -> set[str]:
        """Строит множество перекрывающихся шинглов заданного размера."""
        words = normalised_text.split()
        if len(words) <= self.shingle_size:
            return {normalised_text} if normalised_text else set()
        return {
            " ".join(words[index : index + self.shingle_size])
            for index in range(len(words) - self.shingle_size + 1)
        }

    def _make_minhash(self, normalised_text: str) -> MinHash:
        """Создаёт MinHash-отпечаток по шинглам текста."""
        minhash = MinHash(num_perm=self._minhash_permutations)
        for shingle in self._shingles(normalised_text):
            minhash.update(shingle.encode("utf-8"))
        return minhash

    def _new_document_id(self, document: NewsDocument) -> str:
        """Создаёт стабильный внутренний ID, если ID новости не задан."""
        if document.document_id is not None:
            return str(document.document_id)
        source = f"{document.url or ''}\n{document.text}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _exact_match(
        self,
        normalised_text: str,
        normalised_url: str | None,
    ) -> DuplicateMatch | None:
        """Проверяет совпадение хэша текста или URL."""
        text_id = self._hash_to_id.get(self._text_hash(normalised_text))
        if text_id is not None:
            document = self._documents[text_id]
            return DuplicateMatch(
                document_id=(
                    document.document_id
                    if document.document_id is not None
                    else text_id
                ),
                level="exact_text",
                score=1.0,
                url=document.url,
            )

        if normalised_url is None:
            return None
        url_id = self._url_to_id.get(normalised_url)
        if url_id is None:
            return None
        document = self._documents[url_id]
        return DuplicateMatch(
            document_id=(
                document.document_id
                if document.document_id is not None
                else url_id
            ),
            level="exact_url",
            score=1.0,
            url=document.url,
        )

    def _near_match(
        self,
        normalised_text: str,
    ) -> DuplicateMatch | None:
        """Ищет кандидата через MinHash-LSH и проверяет точный Jaccard."""
        minhash = self._make_minhash(normalised_text)
        candidate_ids = self._lsh.query(minhash)
        current_shingles = self._shingles(normalised_text)
        best_match: DuplicateMatch | None = None
        for candidate_id in candidate_ids:
            candidate_shingles = self._shingles(
                self._normalise_text(self._documents[candidate_id].text)
            )
            union = current_shingles | candidate_shingles
            score = (
                len(current_shingles & candidate_shingles) / len(union)
                if union
                else 1.0
            )
            if score < self.near_duplicate_threshold:
                continue
            document = self._documents[candidate_id]
            match = DuplicateMatch(
                document_id=(
                    document.document_id
                    if document.document_id is not None
                    else candidate_id
                ),
                level="near_text",
                score=score,
                url=document.url,
            )
            if best_match is None or match.score > best_match.score:
                best_match = match
        return best_match

    def _semantic_match(self, text: str) -> DuplicateMatch | None:
        """Ищет самый похожий документ по косинусной близости эмбеддингов."""
        if not self._embeddings:
            return None
        embedding = self._model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        document_ids = list(self._embeddings)
        matrix = np.vstack([self._embeddings[doc_id] for doc_id in document_ids])
        scores = matrix @ embedding
        best_index = int(np.argmax(scores))
        best_score = float(scores[best_index])
        if best_score < self.semantic_duplicate_threshold:
            return None
        document_id = document_ids[best_index]
        document = self._documents[document_id]
        return DuplicateMatch(
            document_id=(
                document.document_id
                if document.document_id is not None
                else document_id
            ),
            level="semantic",
            score=best_score,
            url=document.url,
        )

    @staticmethod
    def _classify_event(text: str) -> str:
        """Заглушка классификации события; позже замените своей моделью."""
        return "unknown"

    @staticmethod
    def _publication_time(document: NewsDocument) -> str:
        """Возвращает время публикации в виде строки ISO 8601."""
        if document.publication_time:
            return document.publication_time
        return datetime.now(timezone.utc).isoformat()

    def _save_unique_news(
        self,
        document: NewsDocument,
        internal_id: str,
    ) -> None:
        """Сохраняет прошедшую дедупликацию новость в SQLite."""
        event_type = document.event_type or self._classify_event(document.text)
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO news (
                    document_id,
                    full_text,
                    publication_time,
                    event_type,
                    url,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    document.text,
                    self._publication_time(document),
                    event_type,
                    document.url,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

    def find_duplicate(self, document: NewsDocument) -> DuplicateMatch | None:
        """Проверяет новость по трём уровням и возвращает найденный дубликат."""
        normalised_text = self._normalise_text(document.text)
        normalised_url = self._normalise_url(document.url)

        exact_match = self._exact_match(normalised_text, normalised_url)
        if exact_match is not None:
            return exact_match

        near_match = self._near_match(normalised_text)
        if near_match is not None:
            return near_match

        return self._semantic_match(document.text)

    def add(self, document: NewsDocument) -> DuplicateMatch | None:
        """Добавляет только уникальную новость и возвращает найденный дубликат."""
        duplicate = self.find_duplicate(document)
        if duplicate is not None:
            return duplicate

        internal_id = self._new_document_id(document)
        if internal_id in self._documents:
            raise ValueError(f"Документ с ID {internal_id!r} уже существует.")

        normalised_text = self._normalise_text(document.text)
        text_hash = self._text_hash(normalised_text)
        minhash = self._make_minhash(normalised_text)
        embedding = self._model.encode(
            document.text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        self._save_unique_news(document, internal_id)

        self._documents[internal_id] = document
        self._embeddings[internal_id] = embedding
        self._hash_to_id[text_hash] = internal_id
        normalised_url = self._normalise_url(document.url)
        if normalised_url is not None:
            self._url_to_id[normalised_url] = internal_id
        self._minhashes[internal_id] = minhash
        self._lsh.insert(internal_id, minhash)
        return None

    def add_many(
        self,
        documents: list[NewsDocument],
    ) -> list[DuplicateMatch | None]:
        """Последовательно добавляет новости и возвращает результат для каждой."""
        return [self.add(document) for document in documents]

    def __len__(self) -> int:
        """Возвращает количество уникальных новостей в индексе."""
        return len(self._documents)