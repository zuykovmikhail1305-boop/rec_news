"""Каскадная дедупликация новостей: от дешёвых проверок к дорогим.

Источник — ``new_summary.db`` (таблица ``news_summary``, её заполняет
``news_summarizer.py``). Каждая новость пачки проверяется по шагам,
дальше идут только не пойманные на предыдущем:

1. ``exact`` — SHA-1 нормализованного полного текста (см.
   :mod:`fingerprints`) совпал с новостью пачки или ``unique.db``;
2. ``url`` — совпал нормализованный URL (без ``utm_*``, якоря, схемы
   и хвостового ``/``);
3. ``id`` — ``document_id`` уже есть в ``unique.db``, хотя текст и URL
   другие (повторная доставка); запись в ``unique.db`` не перезаписывается;
4. ``near`` — почти полная копия: MinHash по шинглам из 4 слов и LSH,
   оценка Жаккара не ниже ``near_threshold``;
5. ``semantic`` — пересказ: похожесть заголовка с первым абзацем
   (:attr:`news_db.ProcessedNews.lead`) — косинус dense-эмбеддингов
   из :meth:`embeddig.QdrantIndex.similarity` — не ниже
   ``duplicate_threshold``. Сравниваются только новости, опубликованные
   не дальше ``time_window_hours`` друг от друга: продолжение сюжета
   через неделю похоже по лиду, но дублем не является.

Каждый шаг сначала ищет оригинал среди новостей пачки (уникальной
остаётся самая ранняя), затем среди уже сохранённых. Хеш и URL
проверяются по индексированным колонкам таблицы ``news_fingerprint``
в ``unique.db``, MinHash — по LSH в памяти (собирается при старте
из той же таблицы), семантика — запросом в Qdrant с фильтром по
``publication_ts`` в payload.

Уникальные новости сохраняются в ``unique.db`` (новости и их отпечатки —
одной транзакцией, без перезаписи), векторы заголовка с первым абзацем —
в коллекцию ``news``, векторы характеристик аудитории — в
``news_audience`` (по ней работает ``recommender.py``). Источники дублей
не теряются: они дописываются к оригиналу (таблица ``news_sources``,
в коде — ``metadata["sources"]``) вместе со способом, которым дубль найден.
После успешной обработки пачки её новости получают в ``new_summary.db``
статус ``ready`` (они не удаляются).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datasketch import MinHash, MinHashLSH
from qdrant_client import models

from news_pipeline.config import SUMMARY_DB, UNIQUE_DB
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.prompts import prompt
from news_pipeline.references import entity_key, source_rating
from news_pipeline.settings import SETTINGS
import numpy as np

from news_pipeline.index.embeddig import QdrantIndex, TextDocument
from news_pipeline.dedup.fingerprints import NUM_PERM, Fingerprint, minhash_from_bytes, minhash_to_bytes
from news_pipeline.storage.news_db import (
    ProcessedNews,
    StoredFingerprint,
    NEW,
    READY,
    append_sources,
    claim_summaries,
    ensure_fingerprint_schema,
    ensure_summary_schema,
    find_by_fingerprint,
    insert_unique_news,
    read_fingerprints,
    release_stale,
    set_summary_status,
    read_summaries,
    upsert_fingerprints,
)
from news_pipeline.recommendation.recommender import audience_document, open_audience_index

# Косинус dense-эмбеддингов заголовка с первым абзацем, подобран на 30 тестовых
# новостях (create_news_db + add_test_news) по парам внутри окна по времени:
# самый слабый дубль — 0.838 (пересказ про метро), самый похожий не-дубль —
# 0.797 (продолжение про ураган через два дня).
DEFAULT_DUPLICATE_THRESHOLD = SETTINGS.dedup["event_cosine"]
# Почти полные копии в тестах дают оценку Жаккара 0.77–0.88, пересказы — до 0.03.
DEFAULT_NEAR_THRESHOLD = 0.75
# Веса ошибок LSH (ложные кандидаты, пропуски): кандидаты всё равно проверяются
# оценкой Жаккара, поэтому важнее не пропустить копию. При пороге 0.75 это
# 16 полос по 8 хешей: копия с Жаккаром 0.85 попадает в кандидаты в 99.4% случаев
# (с весами по умолчанию — в 87%), новость с Жаккаром 0.5 — в 6%.
LSH_WEIGHTS = (0.2, 0.8)
DEFAULT_TIME_WINDOW_HOURS = SETTINGS.dedup["event_window_hours"]
# Подтверждение смысловой склейки (ТЗ §5.2, шаг 4) и «развитие события» (шаг 5).
DEFAULT_ENTITY_JACCARD = SETTINGS.dedup["entity_jaccard"]
DEFAULT_DEVELOPMENT_COSINE = SETTINGS.dedup["development_cosine"]
DEFAULT_DEVELOPMENT_WINDOW_DAYS = SETTINGS.dedup["development_window_days"]
# Косинус, при котором склейка не требует подтверждения сущностями и числами.
CERTAIN_COSINE = 0.93

PUBLICATION_TS_FIELD = "publication_ts"
METHODS = ("exact", "url", "id", "near", "semantic")


def publication_timestamp(value: Any) -> float | None:
    """Unix-время публикации из ISO-строки (без часового пояса — UTC)."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


_SAME_EVENT_PROMPT = prompt("news.same_event")

# Числа, которые встречаются почти в любой новости и ничего не подтверждают.
_TRIVIAL_NUMBERS = {str(n) for n in range(0, 10)} | {str(y) for y in range(2020, 2031)}


def _features(source: Any) -> tuple[set[str], set[str]] | None:
    """Сущности и значимые числа новости — из ``metadata.enrichment`` или payload Qdrant."""
    if not isinstance(source, dict):
        return None
    data = source.get("enrichment") if "enrichment" in source else source
    if not isinstance(data, dict) or "entities" not in data:
        return None
    entities = {entity_key(str(entity)) for entity in data.get("entities") or []} - {""}
    numbers = {str(number) for number in data.get("numbers") or []} - _TRIVIAL_NUMBERS
    return entities, numbers


def _time_filter(start: float, end: float) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key=PUBLICATION_TS_FIELD, range=models.Range(gte=start, lte=end))]
    )


@dataclass(frozen=True)
class DuplicateMatch:
    """Информация о найденном документе-дубликате.

    ``document_id`` — оригинал, который остаётся в ``unique.db``.
    ``level`` равен ``"batch"``, если оригинал из той же пачки, и
    ``"unique"``, если из уже сохранённых новостей. ``method`` — шаг
    каскада, на котором пойман дубль: ``exact``, ``url``, ``id``, ``near``
    или ``semantic``. ``score`` — 1.0 для ``exact``/``url``/``id``, оценка
    Жаккара для ``near`` и косинус dense-эмбеддингов для ``semantic``.
    ``via`` — новость пачки,
    с которой совпал дубль, если она сама оказалась дублем ``document_id``.
    """

    document_id: str | int
    level: str
    method: str
    score: float
    url: str | None
    via: str | None = None
    event_id: str | None = None
    confirmed_by: str | None = None


class NewsDeduplicator:
    """Каскад дедупликации: хеш текста, URL, MinHash LSH, dense-эмбеддинги в Qdrant."""

    def __init__(
        self,
        *,
        source_database_path: str | Path = SUMMARY_DB,
        unique_database_path: str | Path = UNIQUE_DB,
        index: QdrantIndex | None = None,
        audience_index: QdrantIndex | None = None,
        qdrant_path: str | Path | None = None,
        collection_name: str = "news",
        duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
        near_threshold: float = DEFAULT_NEAR_THRESHOLD,
        time_window_hours: float | None = DEFAULT_TIME_WINDOW_HOURS,
        entity_jaccard: float = DEFAULT_ENTITY_JACCARD,
        development_cosine: float = DEFAULT_DEVELOPMENT_COSINE,
        development_window_days: float = DEFAULT_DEVELOPMENT_WINDOW_DAYS,
        llm: LLM | None = None,
        use_llm: bool = True,
        verbose: bool = True,
    ) -> None:
        """Подключает базы данных, общую базу Qdrant с двумя коллекциями и собирает LSH.

        Если ``index`` не передан, создаётся :class:`QdrantIndex`: на сервере ``REC_NEWS_QDRANT_URL``
        или в локальной базе ``qdrant_path`` (по умолчанию ``data/qdrant/storage``).
        Коллекция характеристик ``news_audience`` открывается тем же
        клиентом Qdrant и той же моделью эмбеддингов.
        ``time_window_hours=None`` отключает окно по времени.
        ``use_llm=False`` — спорные пары семантического шага не склеиваются
        (приоритет precision, ТЗ §5.2).
        """
        if not 0.0 < duplicate_threshold <= 1.0:
            raise ValueError("duplicate_threshold должен быть в диапазоне (0, 1].")
        if not 0.0 < near_threshold <= 1.0:
            raise ValueError("near_threshold должен быть в диапазоне (0, 1].")
        if time_window_hours is not None and time_window_hours <= 0:
            raise ValueError("time_window_hours должен быть положительным или None.")
        self.duplicate_threshold = duplicate_threshold
        self.near_threshold = near_threshold
        self.time_window = None if time_window_hours is None else time_window_hours * 3600.0
        self.entity_jaccard = entity_jaccard
        self.development_cosine = development_cosine
        self.development_window = development_window_days * 86400.0
        self._llm = llm
        self.use_llm = use_llm
        self._source_database_path = Path(source_database_path)
        self._unique_database_path = Path(unique_database_path)
        if index is None:  # сервер REC_NEWS_QDRANT_URL (Docker) или локальная база (index/qdrant.py)
            index = QdrantIndex(collection_name, qdrant_path=qdrant_path)
        self.index = index
        self.audience_index = audience_index or open_audience_index(
            qdrant_path=qdrant_path, client=index.client, model=index.model
        )
        self.verbose = verbose
        ensure_summary_schema(self._source_database_path)
        ensure_fingerprint_schema(self._unique_database_path)
        self.index.ensure_payload_index(PUBLICATION_TS_FIELD, models.PayloadSchemaType.FLOAT)
        self._lsh = self._new_lsh()
        self._minhashes: dict[str, MinHash] = {}
        self._sync_with_unique_database()

    # --- Синхронизация с unique.db ----------------------------------------

    def _sync_with_unique_database(self) -> None:
        """Догоняет отпечатки, LSH и Qdrant до содержимого unique.db.

        Нужна при первом запуске на уже заполненной unique.db, после сбоя
        между записью в unique.db и записью в Qdrant и для баз, созданных
        до появления отпечатков и окна по времени. Повторная индексация
        безопасна: точки перезаписываются.
        """
        documents = read_summaries(self._unique_database_path)
        fingerprints = {fp.document_id: fp for fp in read_fingerprints(self._unique_database_path)}
        missing = [document for document in documents if document.document_id not in fingerprints]
        if missing:
            self._print(f"Считаю отпечатки для {len(missing)} новостей unique.db.")
            stored = [self._stored_fingerprint(doc, Fingerprint.of(doc.text, doc.url)) for doc in missing]
            upsert_fingerprints(self._unique_database_path, stored)
            fingerprints.update((fp.document_id, fp) for fp in stored)
        for fingerprint in fingerprints.values():
            self._remember_minhash(fingerprint.document_id, minhash_from_bytes(fingerprint.minhash))

        if self.index.count() < len(documents):
            self._print(
                f"В коллекции news {self.index.count()} векторов, в unique.db "
                f"{len(documents)} новостей. Переиндексация выжимок."
            )
            self.index.index([self._text_document(document) for document in documents])
        without_time = models.Filter(
            must=[models.IsEmptyCondition(is_empty=models.PayloadField(key=PUBLICATION_TS_FIELD))]
        )
        if self.index.count(without_time):
            self._print("Дописываю время публикации в payload коллекции news.")
            for document in documents:
                timestamp = publication_timestamp(document.publication_time)
                if timestamp is not None:
                    self.index.set_payload(document.document_id, {PUBLICATION_TS_FIELD: timestamp})

        audiences = [doc for doc in map(audience_document, documents) if doc is not None]
        if self.audience_index.count() < len(audiences):
            self._print(
                f"В коллекции news_audience {self.audience_index.count()} векторов, "
                f"ожидается {len(audiences)}. Переиндексация характеристик."
            )
            self.audience_index.index(audiences)

    def _new_lsh(self) -> MinHashLSH:
        return MinHashLSH(threshold=self.near_threshold, num_perm=NUM_PERM, weights=LSH_WEIGHTS)

    def _remember_minhash(self, document_id: str, minhash: MinHash) -> None:
        if document_id in self._minhashes or minhash.is_empty():
            return
        self._lsh.insert(document_id, minhash)
        self._minhashes[document_id] = minhash

    # --- Обработка ----------------------------------------------------------

    def process_pending_news(self) -> list[DuplicateMatch | None]:
        """Проверяет новости ``new_summary.db`` со статусом ``new`` и ставит им ``ready``.

        Снимок записей ``new`` получает статус ``processing``. Уникальные
        новости сохраняются в ``unique.db`` и Qdrant, и только после успешной
        обработки всего снимка его записи получают ``ready``. Записи,
        добавленные во время обработки, обработаются следующим запуском.
        Если обработка завершится исключением, снимок возвращается в ``new``.
        """
        release_stale(self._source_database_path, "news_summary")
        documents = claim_summaries(self._source_database_path)
        document_ids = [document.document_id for document in documents]
        self._print(f"Найдено новых новостей: {len(documents)}. ID снимка: {document_ids}")
        try:
            results = self.add_many(documents)
        except BaseException:
            set_summary_status(self._source_database_path, document_ids, NEW)
            raise
        if document_ids:
            updated = set_summary_status(self._source_database_path, document_ids, READY)
            self._print(f"Статус ready в {self._source_database_path.name}: {updated} записей")
        by_method = Counter(result.method for result in results if result is not None)
        self._print(
            f"Обработка завершена: всего={len(results)}, "
            f"уникальных={sum(result is None for result in results)}, "
            f"дубликатов={sum(by_method.values())} "
            f"({', '.join(f'{method}={by_method[method]}' for method in METHODS)})"
        )
        return results

    def add_many(self, documents: list[ProcessedNews]) -> list[DuplicateMatch | None]:
        """Проверяет пачку новостей каскадом и сохраняет уникальные.

        Возвращает результат для каждой новости в исходном порядке:
        ``None`` для уникальной, ``DuplicateMatch`` для дубликата.
        """
        if not documents:
            return []

        timestamps = [publication_timestamp(document.publication_time) for document in documents]
        order = sorted(
            range(len(documents)),
            key=lambda i: float("inf") if timestamps[i] is None else timestamps[i],
        )
        ordered = [documents[index] for index in order]
        times = [timestamps[index] for index in order]
        fingerprints = [Fingerprint.of(document.text, document.url) for document in ordered]

        # Шаги exact, url, id, near: без эмбеддингов
        matches, originals = self._find_cheap_duplicates(ordered, fingerprints)

        # Шаг semantic: векторы считаются только для оставшихся новостей
        survivors = [position for position, match in enumerate(matches) if match is None]
        vectors = dict(
            zip(
                survivors,
                self.index.encode([ordered[position].lead for position in survivors]),
            )
        )
        kept: list[int] = []
        for position in survivors:
            match, original = self._find_batch_semantic(position, kept, ordered, times, vectors)
            if match is None:
                match = self._find_unique_semantic(vectors[position], times[position], ordered[position])
            if match is None:
                kept.append(position)
            else:
                matches[position] = match
                if original is not None:
                    originals[position] = original

        self._resolve_chains(ordered, matches, originals)
        events = self._assign_events(ordered, times, vectors, matches, originals, kept)
        for document, match in zip(ordered, matches):
            if match is not None:
                self._print_duplicate(document, match)

        self._save_unique_news(ordered, fingerprints, vectors, matches, events)

        results: list[DuplicateMatch | None] = [None] * len(documents)
        for position, index in enumerate(order):
            results[index] = matches[position]
        return results

    def add(self, document: ProcessedNews) -> DuplicateMatch | None:
        """Проверяет одну новость и сохраняет её, если она уникальна."""
        return self.add_many([document])[0]

    def find_duplicate(self, document: ProcessedNews) -> DuplicateMatch | None:
        """Проверяет одну новость по уже сохранённым уникальным, ничего не сохраняя."""
        match = self._find_cheap_unique(document, Fingerprint.of(document.text, document.url))
        if match is not None:
            return match
        return self._find_unique_semantic(
            self.index.encode([document.lead])[0], publication_timestamp(document.publication_time), document
        )

    # --- Дешёвые шаги -------------------------------------------------------

    def _find_cheap_duplicates(
        self,
        documents: list[ProcessedNews],
        fingerprints: list[Fingerprint],
    ) -> tuple[list[DuplicateMatch | None], dict[int, int]]:
        """Шаги exact, url, id и near для пачки, отсортированной по времени.

        Возвращает совпадения и для дублей внутри пачки — позицию оригинала.
        """
        matches: list[DuplicateMatch | None] = []
        originals: dict[int, int] = {}
        by_hash: dict[str, int] = {}
        by_url: dict[str, int] = {}
        by_id: dict[str, int] = {}
        batch_lsh = self._new_lsh()
        batch_minhashes: dict[int, MinHash] = {}

        for position, (document, fingerprint) in enumerate(zip(documents, fingerprints)):
            original: int | None = None
            method = score = None
            if fingerprint.text_hash in by_hash:
                original, method, score = by_hash[fingerprint.text_hash], "exact", 1.0
            elif fingerprint.url_key is not None and fingerprint.url_key in by_url:
                original, method, score = by_url[fingerprint.url_key], "url", 1.0
            elif document.document_id in by_id:
                original, method, score = by_id[document.document_id], "id", 1.0

            if original is not None:
                match = self._batch_match(documents[original], method, score)
            else:
                match = self._find_cheap_unique(document, fingerprint, skip_near=True)
            if match is None and not fingerprint.minhash.is_empty():
                best = self._best_jaccard(fingerprint.minhash, batch_lsh, batch_minhashes)
                if best is not None:
                    original = best[0]
                    match = self._batch_match(documents[original], "near", best[1])
                else:
                    match = self._find_unique_near(fingerprint.minhash)

            matches.append(match)
            if match is not None:
                if match.level == "batch":
                    originals[position] = original
                continue
            by_hash[fingerprint.text_hash] = position
            if fingerprint.url_key is not None:
                by_url[fingerprint.url_key] = position
            by_id[document.document_id] = position
            if not fingerprint.minhash.is_empty():
                batch_lsh.insert(position, fingerprint.minhash)
                batch_minhashes[position] = fingerprint.minhash
        return matches, originals

    def _find_cheap_unique(
        self,
        document: ProcessedNews,
        fingerprint: Fingerprint,
        *,
        skip_near: bool = False,
    ) -> DuplicateMatch | None:
        """Шаги exact, url, id (и near, если не ``skip_near``) по unique.db."""
        for method, condition in (
            ("exact", {"text_hash": fingerprint.text_hash}),
            ("url", {"url_key": fingerprint.url_key}),
            ("id", {"document_id": document.document_id}),
        ):
            document_id = find_by_fingerprint(self._unique_database_path, **condition)
            if document_id is not None:
                if method == "id":
                    self._print(
                        f"ВНИМАНИЕ: ID={document_id!r} уже есть в unique.db с другим текстом "
                        f"и URL. Сохранённая запись не перезаписывается."
                    )
                return self._unique_match(document_id, method, 1.0)
        if skip_near or fingerprint.minhash.is_empty():
            return None
        return self._find_unique_near(fingerprint.minhash)

    def _find_unique_near(self, minhash: MinHash) -> DuplicateMatch | None:
        """Шаг near по LSH уже сохранённых уникальных новостей."""
        best = self._best_jaccard(minhash, self._lsh, self._minhashes)
        return None if best is None else self._unique_match(best[0], "near", best[1])

    def _best_jaccard(
        self,
        minhash: MinHash,
        lsh: MinHashLSH,
        minhashes: dict[Any, MinHash],
    ) -> tuple[Any, float] | None:
        """Кандидат LSH с наибольшей оценкой Жаккара не ниже порога."""
        best: tuple[Any, float] | None = None
        for key in lsh.query(minhash):
            jaccard = float(minhash.jaccard(minhashes[key]))
            if jaccard >= self.near_threshold and (best is None or jaccard > best[1]):
                best = (key, jaccard)
        return best

    # --- Семантический шаг --------------------------------------------------

    def _within_window(self, first: float | None, second: float | None) -> bool:
        if self.time_window is None or first is None or second is None:
            return True
        return abs(first - second) <= self.time_window

    def _find_batch_semantic(
        self,
        position: int,
        kept: list[int],
        documents: list[ProcessedNews],
        times: list[float | None],
        vectors: dict[int, np.ndarray],
    ) -> tuple[DuplicateMatch | None, int | None]:
        """Шаг semantic внутри пачки: самая похожая из оставленных в окне, с подтверждением."""
        scored = []
        for candidate in kept:
            if not self._within_window(times[position], times[candidate]):
                continue
            score = self.index.similarity(vectors[candidate], vectors[position])
            if score >= self.duplicate_threshold:
                scored.append((score, candidate))
        for score, candidate in sorted(scored, reverse=True):
            original = documents[candidate]
            confirmed_by = self._confirm_same_event(
                documents[position], _features(original.metadata), original.lead, score,
                pair_key=f"{original.document_id}|{documents[position].document_id}",
            )
            if confirmed_by is None:
                continue
            match = DuplicateMatch(
                document_id=original.document_id,
                level="batch",
                method="semantic",
                score=score,
                url=original.url,
                confirmed_by=confirmed_by,
            )
            return match, candidate
        return None, None

    def _find_unique_semantic(
        self, vector: np.ndarray, timestamp: float | None, document: ProcessedNews | None = None
    ) -> DuplicateMatch | None:
        """Шаг semantic по уникальным новостям в Qdrant в окне по времени, с подтверждением."""
        query_filter = None
        if self.time_window is not None and timestamp is not None:
            query_filter = _time_filter(timestamp - self.time_window, timestamp + self.time_window)
        for candidate in self.index.find_similar(vector, limit=3, query_filter=query_filter):
            if candidate.score < self.duplicate_threshold:
                break
            confirmed_by = "cosine"
            if document is not None:
                confirmed_by = self._confirm_same_event(
                    document, _features(candidate.payload), str(candidate.payload.get("text") or ""),
                    candidate.score, pair_key=f"{candidate.id}|{document.document_id}",
                )
            if confirmed_by is None:
                continue
            return DuplicateMatch(
                document_id=candidate.id,
                level="unique",
                method="semantic",
                score=candidate.score,
                url=candidate.payload.get("url"),
                event_id=candidate.payload.get("event_id"),
                confirmed_by=confirmed_by,
            )
        return None

    # --- Подтверждение склейки и события ------------------------------------

    def _confirm_same_event(
        self,
        document: ProcessedNews,
        other: tuple[set[str], set[str]] | None,
        other_lead: str,
        cosine: float,
        *,
        pair_key: str,
    ) -> str | None:
        """Подтверждает, что пара с высоким косинусом — одно событие (ТЗ §5.2, шаг 4).

        Возвращает, чем подтверждено (``cosine``, ``entities``, ``llm``), или
        ``None``, если пару склеивать нельзя. Совпали сущности (Жаккар не ниже
        порога) и числа не противоречат — склейка; нет ни общих сущностей, ни
        общих чисел — разные события; остальное спорно и решается LLM, а без
        LLM пара не склеивается: ошибочно склеить хуже, чем пропустить дубль.
        """
        own = _features(document.metadata)
        if cosine >= CERTAIN_COSINE or own is None or other is None:
            return "cosine"
        entities_a, numbers_a = own
        entities_b, numbers_b = other
        union = entities_a | entities_b
        entity_ok = bool(union) and len(entities_a & entities_b) / len(union) >= self.entity_jaccard
        numbers_ok: bool | None = None
        if numbers_a and numbers_b:
            numbers_ok = len(numbers_a & numbers_b) / min(len(numbers_a), len(numbers_b)) >= 0.5
        if entity_ok and numbers_ok is not False:
            return "entities"
        if not entity_ok and numbers_ok is False:
            return None
        return "llm" if self._llm_same_event(document.lead, other_lead, pair_key) else None

    def _llm_same_event(self, first: str, second: str, pair_key: str) -> bool:
        if not self.use_llm or not first or not second:
            return False
        llm = self._llm or default_llm()
        try:
            data = llm.json(
                "same_event",
                [{"role": "system", "content": _SAME_EVENT_PROMPT},
                 {"role": "user", "content": f"Новость А:\n{first}\n\nНовость Б:\n{second}"}],
                schema={"type": "object", "properties": {"same_event": {"type": "boolean"}, "reason": {"type": "string"}},
                        "required": ["same_event", "reason"]},
                cache_key=pair_key, max_tokens=600,
            )
        except (LLMUnavailable, LLMError):
            return False
        return bool(data.get("same_event"))

    def _assign_events(
        self,
        documents: list[ProcessedNews],
        times: list[float | None],
        vectors: dict[int, np.ndarray],
        matches: list[DuplicateMatch | None],
        originals: dict[int, int],
        kept: list[int],
    ) -> dict[int, dict[str, str | None]]:
        """``event_id`` и ``parent_event_id`` для каждой новости пачки (ТЗ §5.2, шаг 5).

        Уникальная новость открывает событие ``e_<document_id>``; если она
        похожа на новость за ``development_window`` (косинус не ниже
        ``development_cosine``, есть общие сущности), это «развитие события» —
        ссылка на родительское. Дубль присоединяется к событию оригинала.
        """
        events: dict[int, dict[str, str | None]] = {}
        for position in kept:
            document = documents[position]
            parent = self._find_parent_event(position, documents, times, vectors, kept, events)
            events[position] = {"event_id": f"e_{document.document_id}", "parent_event_id": parent}
        for position, match in enumerate(matches):
            if match is None:
                continue
            original = originals.get(position)
            while original is not None and original in originals:
                original = originals[original]
            if original is not None and original in events:
                event_id = events[original]["event_id"]
            else:
                event_id = match.event_id or self._stored_event_id(str(match.document_id))
            events[position] = {"event_id": event_id, "parent_event_id": None}
            matches[position] = replace(match, event_id=event_id)
        return events

    def _find_parent_event(
        self,
        position: int,
        documents: list[ProcessedNews],
        times: list[float | None],
        vectors: dict[int, np.ndarray],
        kept: list[int],
        events: dict[int, dict[str, str | None]],
    ) -> str | None:
        """Родительское событие для «развития»: ранняя похожая новость с общими сущностями."""
        own = _features(documents[position].metadata)
        if own is None or not own[0]:
            return None
        timestamp = times[position]
        best: tuple[float, str] | None = None
        for candidate in kept:
            if candidate == position or candidate not in events:
                continue
            if timestamp is not None and times[candidate] is not None and not (
                0 <= timestamp - times[candidate] <= self.development_window
            ):
                continue
            score = self.index.similarity(vectors[candidate], vectors[position])
            other = _features(documents[candidate].metadata)
            if score >= self.development_cosine and other and own[0] & other[0]:
                if best is None or score > best[0]:
                    best = (score, events[candidate]["event_id"])
        query_filter = None
        if timestamp is not None:
            query_filter = _time_filter(timestamp - self.development_window, timestamp)
        for candidate in self.index.find_similar(vectors[position], limit=3, query_filter=query_filter):
            other = _features(candidate.payload)
            if candidate.score >= self.development_cosine and other and own[0] & other[0]:
                if best is None or candidate.score > best[0]:
                    best = (candidate.score, candidate.payload.get("event_id") or f"e_{candidate.id}")
                break
        return best[1] if best else None

    def _stored_event_id(self, document_id: str) -> str:
        stored = read_summaries(self._unique_database_path, [document_id])
        if stored and stored[0].metadata.get("event_id"):
            return str(stored[0].metadata["event_id"])
        return f"e_{document_id}"

    # --- Совпадения и сохранение --------------------------------------------

    @staticmethod
    def _batch_match(original: ProcessedNews, method: str, score: float) -> DuplicateMatch:
        return DuplicateMatch(
            document_id=original.document_id, level="batch", method=method, score=score, url=original.url
        )

    def _unique_match(self, document_id: str, method: str, score: float) -> DuplicateMatch:
        stored = read_summaries(self._unique_database_path, [document_id])
        return DuplicateMatch(
            document_id=document_id,
            level="unique",
            method=method,
            score=score,
            url=stored[0].url if stored else None,
        )

    @staticmethod
    def _resolve_chains(
        documents: list[ProcessedNews],
        matches: list[DuplicateMatch | None],
        originals: dict[int, int],
    ) -> None:
        """Перенаправляет дубли на итоговый оригинал.

        Новость могла совпасть (например, по MinHash) с новостью пачки,
        которую потом семантический шаг сам признал дублем. Тогда оригиналом
        становится то, с чем совпала она, а промежуточная новость — ``via``.
        """
        for position in sorted(originals):
            direct = originals[position]
            root = direct
            while root in originals:
                root = originals[root]
            match = matches[position]
            if matches[root] is not None:
                # Корень цепочки — дубль уже сохранённой новости
                final = matches[root]
                matches[position] = replace(
                    match, document_id=final.document_id, level=final.level, url=final.url,
                    via=documents[direct].document_id,
                )
            elif root != direct:
                matches[position] = replace(
                    match, document_id=documents[root].document_id, url=documents[root].url,
                    via=documents[direct].document_id,
                )

    @staticmethod
    def _source_entry(document: ProcessedNews, match: DuplicateMatch | None = None) -> dict[str, Any]:
        """Запись источника для ``metadata["sources"]`` оригинала."""
        entry: dict[str, Any] = {
            "document_id": document.document_id,
            "url": document.url,
            "source": document.metadata.get("source"),
            "publication_time": document.publication_time,
            "method": "original" if match is None else match.method,
        }
        if match is not None:
            entry["score"] = round(match.score, 4)
            if match.via is not None:
                entry["via"] = match.via
        return entry

    @staticmethod
    def _stored_fingerprint(document: ProcessedNews, fingerprint: Fingerprint) -> StoredFingerprint:
        return StoredFingerprint(
            document_id=document.document_id,
            text_hash=fingerprint.text_hash,
            url_key=fingerprint.url_key,
            minhash=minhash_to_bytes(fingerprint.minhash),
        )

    def _save_unique_news(
        self,
        documents: list[ProcessedNews],
        fingerprints: list[Fingerprint],
        vectors: dict[int, np.ndarray],
        matches: list[DuplicateMatch | None],
        events: dict[int, dict[str, str | None]],
    ) -> None:
        """Сохраняет уникальные новости в unique.db и Qdrant, источники дублей — к оригиналам."""
        sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for document, match in zip(documents, matches):
            if match is not None:
                sources[str(match.document_id)].append(self._source_entry(document, match))

        unique = [position for position, match in enumerate(matches) if match is None]
        deduplicated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved = []
        for position in unique:
            document = documents[position]
            own = [self._source_entry(document)] + sources.pop(document.document_id, [])
            saved.append(
                replace(
                    document,
                    metadata={
                        **document.metadata,
                        "publication_time": document.publication_time or deduplicated_at,
                        "deduplicated_at": deduplicated_at,
                        "sources": own,
                        **events.get(position, {"event_id": f"e_{document.document_id}", "parent_event_id": None}),
                    },
                )
            )
        if saved:
            insert_unique_news(
                self._unique_database_path,
                saved,
                [self._stored_fingerprint(doc, fingerprints[pos]) for doc, pos in zip(saved, unique)],
            )
            self.index.index(
                [self._text_document(document) for document in saved],
                dense_vectors=[vectors[position] for position in unique],
            )
            audiences = [doc for doc in map(audience_document, saved) if doc is not None]
            self.audience_index.index(audiences)
            for document, position in zip(saved, unique):
                self._remember_minhash(document.document_id, fingerprints[position].minhash)
                self._print(f"Сохранено в unique.db и Qdrant: ID={document.document_id!r}")
        if sources:
            append_sources(self._unique_database_path, dict(sources))

    @staticmethod
    def _text_document(document: ProcessedNews) -> TextDocument:
        """Документ для индекса: вектор — по лиду, в payload — всё, что нужно ленте."""
        enrichment = document.metadata.get("enrichment") or {}
        topics = enrichment.get("topics") or {}
        source = document.metadata.get("source")
        return TextDocument(
            text=document.lead,
            id=document.document_id,
            payload={
                "document_id": document.document_id,
                "title": document.title,
                "url": document.url,
                "source": source,
                "source_rating": source_rating(source),
                "publication_time": document.publication_time,
                PUBLICATION_TS_FIELD: publication_timestamp(document.publication_time),
                "first_paragraph": document.first_paragraph,
                "event_id": document.metadata.get("event_id") or f"e_{document.document_id}",
                "parent_event_id": document.metadata.get("parent_event_id"),
                "is_representative": True,
                "entities": enrichment.get("entities") or [],
                "numbers": enrichment.get("numbers") or [],
                "topics": topics,
                "topic_names": list(topics),
                "importance": int(enrichment.get("importance") or 3),
                "event_type": enrichment.get("event_type"),
            },
        )

    def _print(self, message: str) -> None:
        """Печатает диагностическое сообщение, если включён verbose-режим."""
        if self.verbose:
            print(f"[NewsDeduplicator] {message}", flush=True)

    def _print_duplicate(self, document: ProcessedNews, match: DuplicateMatch) -> None:
        """Печатает, на каком шаге и с чем совпал дубликат."""
        where = "в пачке" if match.level == "batch" else "среди уникальных"
        details = f"{match.score:.4f}"
        via = f" через {match.via!r}" if match.via is not None else ""
        if match.confirmed_by is not None:
            details += f", подтверждено: {match.confirmed_by}"
        self._print(
            f"Дубликат [{match.method}] {where}: ID={document.document_id!r}, "
            f"совпадение={details}, оригинал ID={match.document_id!r}{via}"
        )

    def close(self) -> None:
        """Закрывает подключение к Qdrant и снимает блокировку папки базы."""
        self.index.close()

    def __len__(self) -> int:
        """Возвращает количество уникальных новостей в Qdrant."""
        return self.index.count()
