"""Дедупликация новостей по семантической близости заголовка и первого абзаца.

Пачка новых новостей из ``news.db`` проходит три шага:

1. проверка новостей пачки между собой;
2. проверка оставшихся новостей с уже уникальными новостями,
   векторы которых лежат в отдельной Qdrant-базе дедупликации;
3. сохранение прошедших проверку новостей в ``unique.db`` и их векторов
   в Qdrant-базу дедупликации.

Qdrant-база дедупликации не связана с гибридным индексом из ``embeddig.py``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL_NAME = "deepvk/USER-bge-m3"
MODELS_DIR = Path(__file__).resolve().parent / "models"


def load_embedding_model(
    model_name: str = EMBEDDING_MODEL_NAME,
    model_path: str | Path | None = None,
) -> SentenceTransformer:
    """Загружает модель из локальной папки, при первом запуске скачивает и сохраняет её."""
    path = Path(model_path) if model_path else MODELS_DIR / model_name.replace("/", "--")
    if (path / "modules.json").exists():
        return SentenceTransformer(str(path))
    model = SentenceTransformer(model_name)
    model.save(str(path))
    return model


@dataclass(frozen=True)
class NewsDocument:
    """Новость, которую нужно проверить и добавить в индекс."""

    text: str
    url: str | None = None
    document_id: str | int | None = None
    publication_time: str | None = None
    event_type: str | None = None
    payload: Mapping[str, Any] | None = None
    title: str | None = None
    first_paragraph: str | None = None


@dataclass(frozen=True)
class DuplicateMatch:
    """Информация о найденном документе-дубликате.

    ``level`` равен ``"batch"``, если дубликат найден среди новостей той же
    пачки, и ``"unique"``, если среди уже сохранённых уникальных новостей.
    """

    document_id: str | int
    level: str
    score: float
    url: str | None


class NewsDeduplicator:
    """Ищет семантические дубликаты по заголовку и первому абзацу."""

    def __init__(
        self,
        *,
        embedding_model: str = EMBEDDING_MODEL_NAME,
        model_path: str | Path | None = None,
        semantic_duplicate_threshold: float = 0.85,
        source_database_path: str = "news.db",
        unique_database_path: str = "unique.db",
        qdrant_path: str | Path = "qdrant_dedup",
        collection_name: str = "unique_news",
        verbose: bool = True,
    ) -> None:
        """Подключает базы данных и загружает модель эмбеддингов."""
        if not 0.0 < semantic_duplicate_threshold <= 1.0:
            raise ValueError(
                "semantic_duplicate_threshold должен быть в диапазоне (0, 1]."
            )

        self.embedding_model_name = embedding_model
        self.semantic_duplicate_threshold = semantic_duplicate_threshold
        self._model = load_embedding_model(embedding_model, model_path)
        self._source_database_path = source_database_path
        self._unique_database_path = unique_database_path
        self._client = QdrantClient(path=str(qdrant_path))
        self._collection_name = collection_name
        self.verbose = verbose
        self._create_database(self._source_database_path)
        self._create_database(self._unique_database_path)
        self._ensure_collection()
        self._sync_collection_with_unique_database()

    def _create_database(self, database_path: str) -> None:
        """Создаёт в указанной SQLite-базе таблицу единого формата."""
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news (
                    document_id TEXT PRIMARY KEY,
                    title TEXT,
                    first_paragraph TEXT,
                    full_text TEXT NOT NULL,
                    publication_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _ensure_collection(self) -> None:
        """Создаёт коллекцию Qdrant для векторов уникальных новостей."""
        if self._client.collection_exists(self._collection_name):
            return
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=models.VectorParams(
                size=self._model.get_embedding_dimension(),
                distance=models.Distance.COSINE,
            ),
        )

    @staticmethod
    def _read_news(database_path: str) -> list[NewsDocument]:
        """Читает все новости из SQLite-базы."""
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT document_id, title, first_paragraph, full_text,
                       publication_time, event_type, url, created_at
                FROM news
                """
            ).fetchall()
        return [
            NewsDocument(
                document_id=row["document_id"],
                title=row["title"],
                first_paragraph=row["first_paragraph"],
                text=row["full_text"],
                publication_time=row["publication_time"],
                event_type=row["event_type"],
                url=row["url"],
            )
            for row in rows
        ]

    def _sync_collection_with_unique_database(self) -> None:
        """Добавляет в Qdrant векторы новостей из unique.db, если их там не хватает.

        Нужна при первом запуске на уже заполненной unique.db и после сбоя
        между записью в unique.db и записью в Qdrant.
        """
        documents = self._read_news(self._unique_database_path)
        stored = self._client.count(self._collection_name, exact=True).count
        if stored >= len(documents):
            return
        self._print(
            f"В Qdrant {stored} векторов, в unique.db {len(documents)} новостей. "
            f"Переиндексация unique.db."
        )
        self._upsert_vectors(documents, self._encode(documents))

    def _delete_processed_news(self, document_ids: list[str]) -> None:
        """Удаляет из общей базы только ID из сохранённого снимка."""
        if not document_ids:
            self._print("Удаление не требуется: news.db пустая.")
            return
        self._print(
            f"Удаление из news.db по ID: {', '.join(document_ids)}"
        )
        with sqlite3.connect(self._source_database_path) as connection:
            connection.executemany(
                "DELETE FROM news WHERE document_id = ?",
                [(document_id,) for document_id in document_ids],
            )
            connection.commit()
            deleted_count = connection.total_changes
        self._print(f"Удалено из news.db записей: {deleted_count}")

    def process_pending_news(self) -> list[DuplicateMatch | None]:
        """Проверяет новые новости и удаляет обработанные записи из news.db.

        Сначала запоминаются ID текущего снимка. Уникальные документы
        сохраняются в ``unique.db`` и Qdrant, и только после успешной обработки
        всего снимка его ID удаляются из ``news.db``. Поэтому записи,
        добавленные в ``news.db`` во время обработки, не попадут в удаление
        и обработаются следующим запуском. Если обработка завершится
        исключением, удаление не выполняется.
        """
        documents = self._read_news(self._source_database_path)
        document_ids = [str(document.document_id) for document in documents]
        self._print(
            f"Найдено новых новостей: {len(documents)}. "
            f"ID снимка: {document_ids}"
        )
        results = self.add_many(documents)
        self._delete_processed_news(document_ids)
        self._print(
            f"Обработка завершена: всего={len(results)}, "
            f"уникальных={sum(result is None for result in results)}, "
            f"дубликатов={sum(result is not None for result in results)}"
        )
        return results

    @staticmethod
    def _comparison_text(document: NewsDocument) -> str:
        """Формирует текст для сравнения из заголовка и первого абзаца."""
        parts = [
            part.strip()
            for part in (document.title, document.first_paragraph)
            if part and part.strip()
        ]
        return " ".join(parts)

    def _encode(self, documents: list[NewsDocument]) -> np.ndarray:
        """Кодирует ``title + first_paragraph`` каждой новости в вектор."""
        return self._model.encode(
            [self._comparison_text(document) for document in documents],
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

    def _new_document_id(self, document: NewsDocument) -> str:
        """Создаёт стабильный внутренний ID, если ID новости не задан."""
        if document.document_id is not None:
            return str(document.document_id)
        source = f"{document.url or ''}\n{document.text}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _point_id(internal_id: str) -> str:
        """Возвращает допустимый для Qdrant ID; исходный ID хранится в payload."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, internal_id))

    def _find_batch_duplicates(
        self,
        documents: list[NewsDocument],
        embeddings: np.ndarray,
    ) -> list[DuplicateMatch | None]:
        """Шаг 1: ищет дубликаты среди новостей одной пачки.

        Новости должны быть отсортированы по времени публикации, тогда
        уникальной остаётся самая ранняя из похожих.
        """
        results: list[DuplicateMatch | None] = []
        kept: list[int] = []
        for index, embedding in enumerate(embeddings):
            match = None
            if kept:
                scores = embeddings[kept] @ embedding
                best = int(np.argmax(scores))
                if scores[best] >= self.semantic_duplicate_threshold:
                    original = documents[kept[best]]
                    match = DuplicateMatch(
                        document_id=self._new_document_id(original),
                        level="batch",
                        score=float(scores[best]),
                        url=original.url,
                    )
            if match is None:
                kept.append(index)
            results.append(match)
        return results

    def _find_unique_duplicate(self, embedding: np.ndarray) -> DuplicateMatch | None:
        """Шаг 2: ищет самый похожий документ среди уникальных новостей в Qdrant."""
        points = self._client.query_points(
            collection_name=self._collection_name,
            query=embedding.tolist(),
            limit=1,
            score_threshold=self.semantic_duplicate_threshold,
            with_payload=True,
        ).points
        if not points:
            return None
        payload = points[0].payload or {}
        return DuplicateMatch(
            document_id=payload.get("document_id", points[0].id),
            level="unique",
            score=points[0].score,
            url=payload.get("url"),
        )

    def find_duplicate(self, document: NewsDocument) -> DuplicateMatch | None:
        """Проверяет одну новость по уже сохранённым уникальным новостям."""
        return self._find_unique_duplicate(self._encode([document])[0])

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

    def _save_unique_news(self, documents: list[NewsDocument]) -> None:
        """Сохраняет прошедшие дедупликацию новости в unique.db."""
        created_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._unique_database_path) as connection:
            connection.executemany(
                """
                INSERT INTO news (
                    document_id,
                    title,
                    first_paragraph,
                    full_text,
                    publication_time,
                    event_type,
                    url,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self._new_document_id(document),
                        document.title,
                        document.first_paragraph,
                        document.text,
                        self._publication_time(document),
                        document.event_type or self._classify_event(document.text),
                        document.url,
                        created_at,
                    )
                    for document in documents
                ],
            )
            connection.commit()

    def _upsert_vectors(
        self,
        documents: list[NewsDocument],
        embeddings: np.ndarray,
    ) -> None:
        """Сохраняет векторы уникальных новостей в Qdrant-базу дедупликации."""
        if not documents:
            return
        points = []
        for document, embedding in zip(documents, embeddings):
            internal_id = self._new_document_id(document)
            points.append(
                models.PointStruct(
                    id=self._point_id(internal_id),
                    vector=embedding.tolist(),
                    payload={
                        "document_id": internal_id,
                        "title": document.title,
                        "url": document.url,
                        "publication_time": self._publication_time(document),
                    },
                )
            )
        self._client.upsert(
            collection_name=self._collection_name, points=points, wait=True
        )

    def _print(self, message: str) -> None:
        """Печатает диагностическое сообщение, если включён verbose-режим."""
        if self.verbose:
            print(f"[NewsDeduplicator] {message}")

    def _print_duplicate(self, document: NewsDocument, match: DuplicateMatch) -> None:
        """Печатает информацию о найденном дубликате."""
        where = "в пачке" if match.level == "batch" else "среди уникальных"
        self._print(
            f"Дубликат {where}: ID={document.document_id!r}, "
            f"совпадение={match.score:.4f}, "
            f"найденный ID={match.document_id!r}"
        )

    def add_many(
        self,
        documents: list[NewsDocument],
    ) -> list[DuplicateMatch | None]:
        """Проверяет пачку новостей и сохраняет уникальные.

        Возвращает результат для каждой новости в исходном порядке:
        ``None`` для уникальной, ``DuplicateMatch`` для дубликата.
        """
        if not documents:
            return []

        order = sorted(
            range(len(documents)),
            key=lambda index: documents[index].publication_time or "",
        )
        ordered = [documents[index] for index in order]
        embeddings = self._encode(ordered)

        # Шаг 1: между собой
        matches = self._find_batch_duplicates(ordered, embeddings)
        for document, match in zip(ordered, matches):
            if match is not None:
                self._print_duplicate(document, match)

        # Шаг 2: с уже сохранёнными уникальными новостями
        for index, document in enumerate(ordered):
            if matches[index] is not None:
                continue
            matches[index] = self._find_unique_duplicate(embeddings[index])
            if matches[index] is not None:
                self._print_duplicate(document, matches[index])

        # Шаг 3: сохранение уникальных в unique.db, затем их векторов в Qdrant
        unique_indexes = [index for index, match in enumerate(matches) if match is None]
        unique_documents = [ordered[index] for index in unique_indexes]
        self._save_unique_news(unique_documents)
        self._upsert_vectors(unique_documents, embeddings[unique_indexes])
        for document in unique_documents:
            self._print(f"Сохранено в unique.db и Qdrant: ID={document.document_id!r}")

        results: list[DuplicateMatch | None] = [None] * len(documents)
        for position, index in enumerate(order):
            results[index] = matches[position]
        return results

    def add(self, document: NewsDocument) -> DuplicateMatch | None:
        """Проверяет одну новость и сохраняет её, если она уникальна."""
        return self.add_many([document])[0]

    def close(self) -> None:
        """Закрывает подключение к Qdrant и снимает блокировку папки базы."""
        self._client.close()

    def __len__(self) -> int:
        """Возвращает количество уникальных новостей в Qdrant."""
        return self._client.count(self._collection_name, exact=True).count
