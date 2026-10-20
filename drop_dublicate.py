"""Дедупликация новостей по LLM-выжимке с гибридной похожестью dense + sparse.

Источник — ``new_summary.db`` (таблица ``news_summary``, её заполняет
``news_summarizer.py``). Сравнивается поле ``summary``. Векторы строит
и хранит :class:`embeddig.QdrantHybridIndex` — это та же коллекция Qdrant,
по которой потом идёт поиск новостей. Похожесть двух новостей —
взвешенная сумма косинусов dense- и BM25 sparse-векторов (см.
:meth:`embeddig.QdrantHybridIndex.similarity`).

Пачка новостей проходит три шага:

1. проверка новостей пачки между собой;
2. проверка оставшихся новостей с уже сохранёнными уникальными в Qdrant;
3. сохранение прошедших проверку новостей в ``unique.db`` (таблица
   ``news_summary``: текст, выжимка, шаблон аудитории, метаданные),
   векторов их выжимок — в коллекцию ``news`` и векторов их характеристик
   аудитории — в коллекцию ``news_audience`` (по ней работает
   ``recommender.py``).

После успешной обработки пачки её новости удаляются из ``new_summary.db``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from embeddig import HybridVector, QdrantHybridIndex, TextDocument, statistics_path
from news_db import (
    SummarizedNews,
    delete_summaries,
    ensure_summary_schema,
    insert_summaries,
    read_summaries,
)
from recommender import audience_document, open_audience_index

DEFAULT_DUPLICATE_THRESHOLD = 0.80
DEFAULT_DENSE_WEIGHT = 0.5


@dataclass(frozen=True)
class DuplicateMatch:
    """Информация о найденном документе-дубликате.

    ``level`` равен ``"batch"``, если дубликат найден среди новостей той же
    пачки, и ``"unique"``, если среди уже сохранённых уникальных новостей.
    ``dense`` и ``sparse`` — косинусы по каждому вектору, ``score`` — итог.
    """

    document_id: str | int
    level: str
    score: float
    dense: float
    sparse: float
    url: str | None


class NewsDeduplicator:
    """Ищет дубликаты по выжимке новости через гибридный индекс Qdrant."""

    def __init__(
        self,
        *,
        source_database_path: str | Path = "new_summary.db",
        unique_database_path: str | Path = "unique.db",
        index: QdrantHybridIndex | None = None,
        audience_index: QdrantHybridIndex | None = None,
        qdrant_path: str | Path = "qdrant_data",
        collection_name: str = "news",
        duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
        dense_weight: float = DEFAULT_DENSE_WEIGHT,
        verbose: bool = True,
    ) -> None:
        """Подключает базы данных и общую базу Qdrant с двумя коллекциями.

        Если ``index`` не передан, создаётся :class:`QdrantHybridIndex`
        в ``qdrant_path`` со статистикой BM25 в файле
        ``<qdrant_path>.<collection_name>.bm25.json`` рядом с базой.
        Коллекция характеристик ``news_audience`` открывается тем же
        клиентом Qdrant и той же моделью эмбеддингов.
        """
        if not 0.0 < duplicate_threshold <= 1.0:
            raise ValueError("duplicate_threshold должен быть в диапазоне (0, 1].")
        self.duplicate_threshold = duplicate_threshold
        self._source_database_path = Path(source_database_path)
        self._unique_database_path = Path(unique_database_path)
        if index is None:
            qdrant_path = Path(qdrant_path)
            index = QdrantHybridIndex(
                collection_name,
                qdrant_path=qdrant_path,
                metadata_path=statistics_path(qdrant_path, collection_name),
                dense_weight=dense_weight,
            )
        self.index = index
        self.audience_index = audience_index or open_audience_index(
            qdrant_path=qdrant_path, client=index.client, model=index.model
        )
        self.verbose = verbose
        ensure_summary_schema(self._source_database_path)
        ensure_summary_schema(self._unique_database_path)
        self._sync_index_with_unique_database()

    def _sync_index_with_unique_database(self) -> None:
        """Добавляет в Qdrant новости из unique.db, если их там не хватает.

        Нужна при первом запуске на уже заполненной unique.db и после сбоя
        между записью в unique.db и записью в Qdrant. Повторная индексация
        безопасна: точки перезаписываются, статистика BM25 не удваивается.
        """
        documents = read_summaries(self._unique_database_path)
        if self.index.count() < len(documents):
            self._print(
                f"В коллекции news {self.index.count()} векторов, в unique.db "
                f"{len(documents)} новостей. Переиндексация выжимок."
            )
            self.index.index([self._text_document(document) for document in documents])
        audiences = [doc for doc in map(audience_document, documents) if doc is not None]
        if self.audience_index.count() < len(audiences):
            self._print(
                f"В коллекции news_audience {self.audience_index.count()} векторов, "
                f"ожидается {len(audiences)}. Переиндексация характеристик."
            )
            self.audience_index.index(audiences)

    def process_pending_news(self) -> list[DuplicateMatch | None]:
        """Проверяет новости из new_summary.db и удаляет обработанные.

        Сначала запоминаются ID текущего снимка. Уникальные новости
        сохраняются в ``unique.db`` и Qdrant, и только после успешной
        обработки всего снимка его ID удаляются из ``new_summary.db``.
        Записи, добавленные во время обработки, обработаются следующим
        запуском. Если обработка завершится исключением, удаление
        не выполняется.
        """
        documents = read_summaries(self._source_database_path)
        document_ids = [document.document_id for document in documents]
        self._print(f"Найдено новых новостей: {len(documents)}. ID снимка: {document_ids}")
        results = self.add_many(documents)
        if document_ids:
            deleted = delete_summaries(self._source_database_path, document_ids)
            self._print(f"Удалено из {self._source_database_path.name} записей: {deleted}")
        self._print(
            f"Обработка завершена: всего={len(results)}, "
            f"уникальных={sum(result is None for result in results)}, "
            f"дубликатов={sum(result is not None for result in results)}"
        )
        return results

    def add_many(self, documents: list[SummarizedNews]) -> list[DuplicateMatch | None]:
        """Проверяет пачку новостей и сохраняет уникальные.

        Возвращает результат для каждой новости в исходном порядке:
        ``None`` для уникальной, ``DuplicateMatch`` для дубликата.
        """
        if not documents:
            return []

        order = sorted(range(len(documents)), key=lambda i: documents[i].publication_time or "")
        ordered = [documents[index] for index in order]
        vectors = self.index.encode([document.summary for document in ordered])

        # Шаг 1: между собой
        matches = self._find_batch_duplicates(ordered, vectors)
        for document, match in zip(ordered, matches):
            if match is not None:
                self._print_duplicate(document, match)

        # Шаг 2: с уже сохранёнными уникальными новостями
        for position, document in enumerate(ordered):
            if matches[position] is not None:
                continue
            matches[position] = self._find_unique_duplicate(vectors[position])
            if matches[position] is not None:
                self._print_duplicate(document, matches[position])

        # Шаг 3: сохранение уникальных в unique.db, затем их векторов в Qdrant
        unique_positions = [position for position, match in enumerate(matches) if match is None]
        self._save_unique_news(
            [ordered[position] for position in unique_positions],
            [vectors[position] for position in unique_positions],
        )

        results: list[DuplicateMatch | None] = [None] * len(documents)
        for position, index in enumerate(order):
            results[index] = matches[position]
        return results

    def add(self, document: SummarizedNews) -> DuplicateMatch | None:
        """Проверяет одну новость и сохраняет её, если она уникальна."""
        return self.add_many([document])[0]

    def find_duplicate(self, document: SummarizedNews) -> DuplicateMatch | None:
        """Проверяет одну новость по уже сохранённым уникальным, ничего не сохраняя."""
        return self._find_unique_duplicate(self.index.encode([document.summary])[0])

    def _find_batch_duplicates(
        self,
        documents: list[SummarizedNews],
        vectors: list[HybridVector],
    ) -> list[DuplicateMatch | None]:
        """Шаг 1: ищет дубликаты среди новостей одной пачки.

        Новости должны быть отсортированы по времени публикации, тогда
        уникальной остаётся самая ранняя из похожих.
        """
        results: list[DuplicateMatch | None] = []
        kept: list[int] = []
        for position, vector in enumerate(vectors):
            best: tuple[float, float, float, int] | None = None
            for candidate in kept:
                dense, sparse, score = self.index.similarity(vectors[candidate], vector)
                if best is None or score > best[2]:
                    best = (dense, sparse, score, candidate)
            match = None
            if best is not None and best[2] >= self.duplicate_threshold:
                original = documents[best[3]]
                match = DuplicateMatch(
                    document_id=original.document_id,
                    level="batch",
                    score=best[2],
                    dense=best[0],
                    sparse=best[1],
                    url=original.url,
                )
            if match is None:
                kept.append(position)
            results.append(match)
        return results

    def _find_unique_duplicate(self, vector: HybridVector) -> DuplicateMatch | None:
        """Шаг 2: ищет самый похожий документ среди уникальных новостей в Qdrant."""
        similar = self.index.find_similar(vector, limit=1)
        if not similar or similar[0].score < self.duplicate_threshold:
            return None
        best = similar[0]
        return DuplicateMatch(
            document_id=best.id,
            level="unique",
            score=best.score,
            dense=best.dense,
            sparse=best.sparse,
            url=best.payload.get("url"),
        )

    def _save_unique_news(
        self,
        documents: list[SummarizedNews],
        vectors: list[HybridVector],
    ) -> None:
        """Сохраняет прошедшие дедупликацию новости в unique.db и Qdrant."""
        if not documents:
            return
        deduplicated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        documents = [
            replace(
                document,
                metadata={
                    **document.metadata,
                    "publication_time": document.publication_time or deduplicated_at,
                    "deduplicated_at": deduplicated_at,
                },
            )
            for document in documents
        ]
        insert_summaries(self._unique_database_path, documents)
        self.index.index(
            [self._text_document(document) for document in documents],
            dense_vectors=[vector.dense for vector in vectors],
        )
        audiences = [doc for doc in map(audience_document, documents) if doc is not None]
        self.audience_index.index(audiences)
        for document in documents:
            self._print(f"Сохранено в unique.db и Qdrant: ID={document.document_id!r}")

    @staticmethod
    def _text_document(document: SummarizedNews) -> TextDocument:
        """Документ для индекса: вектор строится по выжимке, в payload — ссылки на источник."""
        return TextDocument(
            text=document.summary,
            id=document.document_id,
            payload={
                "document_id": document.document_id,
                "title": document.title,
                "url": document.url,
                "publication_time": document.publication_time,
                "summary": document.summary,
            },
        )

    def _print(self, message: str) -> None:
        """Печатает диагностическое сообщение, если включён verbose-режим."""
        if self.verbose:
            print(f"[NewsDeduplicator] {message}", flush=True)

    def _print_duplicate(self, document: SummarizedNews, match: DuplicateMatch) -> None:
        """Печатает информацию о найденном дубликате."""
        where = "в пачке" if match.level == "batch" else "среди уникальных"
        self._print(
            f"Дубликат {where}: ID={document.document_id!r}, "
            f"совпадение={match.score:.4f} (dense={match.dense:.3f}, sparse={match.sparse:.3f}), "
            f"найденный ID={match.document_id!r}"
        )

    def close(self) -> None:
        """Закрывает подключение к Qdrant и снимает блокировку папки базы."""
        self.index.close()

    def __len__(self) -> int:
        """Возвращает количество уникальных новостей в Qdrant."""
        return self.index.count()
