"""Гибридный индекс новостей в Qdrant: dense- и BM25 sparse-векторы.

Каждый документ хранится в одной коллекции с двумя векторами:

* ``dense`` — эмбеддинг ``deepvk/USER-bge-m3``, сравнение по косинусу;
* ``sparse`` — BM25-вектор, нормированный по L2, поэтому скалярное
  произведение в Qdrant тоже равно косинусу.

Статистика BM25 (словарь, документные частоты, средняя длина) копится
инкрементально и хранится в JSON рядом с базой: новые документы
добавляются без пересчёта старых векторов, а номера слов не меняются.

Индекс используется в двух сценариях:

* :meth:`QdrantHybridIndex.search` — поиск по запросу с объединением
  рангов dense и sparse через RRF;
* :meth:`QdrantHybridIndex.find_similar` и
  :meth:`QdrantHybridIndex.similarity` — оценка похожести документов для
  дедупликации: ``dense_weight * dense + (1 - dense_weight) * sparse``.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL_NAME = "deepvk/USER-bge-m3"
MODELS_DIR = Path(__file__).resolve().parent / "models"
STATISTICS_VERSION = 2


def statistics_path(qdrant_path: str | Path, collection_name: str) -> Path:
    """Файл статистики BM25 коллекции рядом с локальной базой Qdrant.

    Например, ``qdrant_data`` + ``news`` -> ``qdrant_data.news.bm25.json``.
    """
    qdrant_path = Path(qdrant_path)
    return qdrant_path.with_name(f"{qdrant_path.name}.{collection_name}.bm25.json")


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
class TextDocument:
    """Текст для индексации и дополнительные данные результата поиска."""

    text: str
    id: str | int | None = None
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class HybridVector:
    """Пара векторов документа: dense (нормирован) и sparse BM25 (нормирован)."""

    dense: np.ndarray
    sparse: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SimilarDocument:
    """Документ из индекса и его похожесть на заданный вектор."""

    id: str | int
    payload: dict[str, Any]
    dense: float
    sparse: float
    score: float


class QdrantHybridIndex:
    """Индексирует и ищет тексты через dense- и BM25 sparse-векторы.

    Параметр ``qdrant_path`` создаёт локальную persistent-базу Qdrant.
    Для удалённого сервера передайте ``url`` и при необходимости ``api_key``.
    Локальную базу одновременно может открыть только один процесс; если
    индекс нужен нескольким процессам сразу, используйте сервер Qdrant.
    """

    _TOKEN_RE = re.compile(r"(?u)\w+")

    def __init__(
        self,
        collection_name: str = "news",
        *,
        qdrant_path: str | Path | None = None,
        url: str | None = None,
        api_key: str | None = None,
        embedding_model: str = EMBEDDING_MODEL_NAME,
        model_path: str | Path | None = None,
        metadata_path: str | Path | None = None,
        client: QdrantClient | None = None,
        dense_weight: float = 0.5,
        model: SentenceTransformer | None = None,
    ) -> None:
        """Создаёт подключение к Qdrant, коллекцию и загружает статистику BM25.

        ``dense_weight`` — вес dense-косинуса в итоговой похожести
        :meth:`similarity`; вес sparse-косинуса равен ``1 - dense_weight``.
        ``client`` и ``model`` позволяют нескольким индексам (коллекциям)
        работать с одной базой Qdrant и одной загруженной моделью.
        """
        if client is not None and (url is not None or qdrant_path is not None):
            raise ValueError("Pass either client or url/qdrant_path, not both.")
        if not 0.0 <= dense_weight <= 1.0:
            raise ValueError("dense_weight должен быть в диапазоне [0, 1].")
        if client is not None:
            self.client = client
        elif url is not None:
            self.client = QdrantClient(url=url, api_key=api_key)
        else:
            self.client = QdrantClient(path=str(qdrant_path or "qdrant_data"))

        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        self.dense_weight = dense_weight
        self._model = model or load_embedding_model(embedding_model, model_path)
        self._vocabulary: dict[str, int] = {}
        self._document_frequency: dict[int, int] = {}
        self._document_count = 0
        self._total_length = 0
        self._counted_ids: set[str] = set()
        self.metadata_path = Path(metadata_path or f"{collection_name}.bm25.json")
        self._load_bm25_metadata()
        self._ensure_collection()

    # --- BM25 -----------------------------------------------------------

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Разбивает текст на нормализованные слова-токены."""
        return QdrantHybridIndex._TOKEN_RE.findall(text.lower())

    def _term_index(self, term: str) -> int:
        """Номер слова в словаре; новые слова добавляются в конец."""
        index = self._vocabulary.get(term)
        if index is None:
            index = len(self._vocabulary)
            self._vocabulary[term] = index
        return index

    def _idf(self, index: int) -> float:
        """IDF слова по накопленной статистике (для невиданного слова — максимальный)."""
        frequency = self._document_frequency.get(index, 0)
        documents = max(self._document_count, frequency)
        return math.log(1.0 + (documents - frequency + 0.5) / (frequency + 0.5))

    def _bm25_vector(
        self,
        tokens: Iterable[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> dict[int, float]:
        """Преобразует токены текста в BM25-вектор, нормированный по L2."""
        counts = Counter(tokens)
        length = sum(counts.values())
        average_length = (
            self._total_length / self._document_count if self._document_count else length
        ) or 1.0
        vector: dict[int, float] = {}
        for term, term_frequency in counts.items():
            index = self._term_index(term)
            denominator = term_frequency + k1 * (1.0 - b + b * length / average_length)
            vector[index] = self._idf(index) * term_frequency * (k1 + 1.0) / denominator
        norm = math.sqrt(sum(value * value for value in vector.values()))
        return {index: value / norm for index, value in vector.items()} if norm else {}

    def _update_statistics(self, documents: Sequence[TextDocument]) -> None:
        """Учитывает документы в статистике BM25 (каждый ID — один раз)."""
        for document in documents:
            key = str(self._point_id(document.id))
            if key in self._counted_ids:
                continue
            tokens = self._tokens(document.text)
            for term in set(tokens):
                index = self._term_index(term)
                self._document_frequency[index] = self._document_frequency.get(index, 0) + 1
            self._document_count += 1
            self._total_length += len(tokens)
            self._counted_ids.add(key)

    def _reset_statistics(self) -> None:
        self._vocabulary = {}
        self._document_frequency = {}
        self._document_count = 0
        self._total_length = 0
        self._counted_ids = set()

    def _load_bm25_metadata(self) -> None:
        """Загружает статистику BM25 из JSON, если она уже существует."""
        if not self.metadata_path.exists():
            return
        with self.metadata_path.open("r", encoding="utf-8") as file:
            metadata = json.load(file)
        if metadata.get("version") != STATISTICS_VERSION:
            raise ValueError(
                f"{self.metadata_path} создан старой версией индекса. Удалите его вместе "
                f"с базой Qdrant и проиндексируйте документы заново."
            )
        if metadata.get("embedding_model") != self.embedding_model_name:
            raise ValueError(
                "The metadata uses a different embedding model: "
                f"{metadata.get('embedding_model')!r}."
            )
        self._vocabulary = {term: int(index) for term, index in metadata["vocabulary"].items()}
        self._document_frequency = {
            int(index): int(value) for index, value in metadata["document_frequency"].items()
        }
        self._document_count = int(metadata["document_count"])
        self._total_length = int(metadata["total_length"])
        self._counted_ids = set(metadata["counted_ids"])

    def _save_bm25_metadata(self) -> None:
        """Сохраняет словарь и статистику BM25 в JSON-файл."""
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "version": STATISTICS_VERSION,
            "embedding_model": self.embedding_model_name,
            "vocabulary": self._vocabulary,
            "document_frequency": self._document_frequency,
            "document_count": self._document_count,
            "total_length": self._total_length,
            "counted_ids": sorted(self._counted_ids),
        }
        with self.metadata_path.open("w", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False)

    # --- Qdrant ---------------------------------------------------------

    def _ensure_collection(self) -> None:
        """Создаёт коллекцию Qdrant с dense-, sparse-векторами и HNSW."""
        if self.client.collection_exists(self.collection_name):
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={
                "dense": models.VectorParams(
                    size=self._model.get_embedding_dimension(),
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
                )
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=False)
                )
            },
        )

    @staticmethod
    def _normalise_documents(
        documents: Sequence[str | TextDocument],
    ) -> list[TextDocument]:
        """Приводит строки и объекты документов к единому формату."""
        result = []
        for index, document in enumerate(documents):
            if isinstance(document, str):
                result.append(TextDocument(text=document, id=index))
            else:
                result.append(
                    TextDocument(
                        text=document.text,
                        id=index if document.id is None else document.id,
                        payload=document.payload,
                    )
                )
        return result

    @staticmethod
    def _point_id(document_id: str | int) -> str | int:
        """Возвращает допустимый для Qdrant ID, сохраняя исходный ID в payload."""
        if isinstance(document_id, int):
            return document_id
        try:
            return str(uuid.UUID(document_id))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, document_id))

    @staticmethod
    def _sparse_model(vector: Mapping[int, float]) -> models.SparseVector:
        return models.SparseVector(indices=list(vector), values=list(vector.values()))

    def index(
        self,
        documents: Sequence[str | TextDocument],
        *,
        recreate: bool = False,
        dense_vectors: np.ndarray | None = None,
    ) -> None:
        """Добавляет документы с двумя векторами в Qdrant.

        Документы сначала учитываются в статистике BM25, затем кодируются.
        Повторная индексация того же ID обновляет точку, но не учитывает
        документ в статистике второй раз. ``recreate=True`` удаляет коллекцию
        и статистику перед загрузкой. ``dense_vectors`` позволяет передать
        уже посчитанные dense-векторы, чтобы не кодировать тексты повторно.
        """
        normalised = self._normalise_documents(documents)
        if not normalised:
            return
        if recreate:
            if self.client.collection_exists(self.collection_name):
                self.client.delete_collection(self.collection_name)
            self._reset_statistics()
            self._ensure_collection()

        self._update_statistics(normalised)
        texts = [document.text for document in normalised]
        if dense_vectors is None:
            dense_vectors = self._encode_dense(texts)
        points = []
        for document, dense in zip(normalised, dense_vectors):
            payload = dict(document.payload or {})
            payload.setdefault("text", document.text)
            payload.setdefault("document_id", document.id)
            points.append(
                models.PointStruct(
                    id=self._point_id(document.id),
                    vector={
                        "dense": np.asarray(dense).tolist(),
                        "sparse": self._sparse_model(self._bm25_vector(self._tokens(document.text))),
                    },
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=self.collection_name, points=points, wait=True)
        self._save_bm25_metadata()

    # --- Похожесть для дедупликации ------------------------------------

    def _encode_dense(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)

    def encode(self, texts: Sequence[str]) -> list[HybridVector]:
        """Кодирует тексты в пары векторов по текущей статистике BM25.

        Статистика не меняется: документы учитываются в ней только при
        :meth:`index`.
        """
        if not texts:
            return []
        dense = self._encode_dense(texts)
        return [
            HybridVector(dense=vector, sparse=self._bm25_vector(self._tokens(text)))
            for vector, text in zip(dense, texts)
        ]

    def similarity(self, first: HybridVector, second: HybridVector) -> tuple[float, float, float]:
        """Возвращает ``(dense, sparse, итог)`` — косинусы и их взвешенную сумму."""
        dense = float(np.dot(first.dense, second.dense))
        small, large = sorted((first.sparse, second.sparse), key=len)
        sparse = float(sum(value * large.get(index, 0.0) for index, value in small.items()))
        return dense, sparse, self.dense_weight * dense + (1.0 - self.dense_weight) * sparse

    def find_similar(
        self,
        vector: HybridVector,
        *,
        limit: int = 5,
        candidates: int = 20,
    ) -> list[SimilarDocument]:
        """Ищет в индексе документы, самые похожие на ``vector``.

        Кандидаты отбираются в Qdrant отдельно по dense- и по sparse-вектору,
        затем для их объединения похожесть пересчитывается точно — по той же
        формуле, что и :meth:`similarity`.
        """
        if self.count() == 0:
            return []
        candidate_ids: dict[Any, None] = {}
        queries: list[tuple[str, Any]] = [("dense", vector.dense.tolist())]
        if vector.sparse:
            queries.append(("sparse", self._sparse_model(vector.sparse)))
        for using, query in queries:
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query,
                using=using,
                limit=candidates,
                with_payload=False,
            )
            candidate_ids.update((point.id, None) for point in response.points)

        stored = self.client.retrieve(
            collection_name=self.collection_name,
            ids=list(candidate_ids),
            with_payload=True,
            with_vectors=True,
        )
        result = []
        for point in stored:
            sparse = point.vector.get("sparse")
            other = HybridVector(
                dense=np.asarray(point.vector["dense"], dtype=np.float32),
                sparse=dict(zip(sparse.indices, sparse.values)) if sparse else {},
            )
            dense_score, sparse_score, score = self.similarity(vector, other)
            result.append(
                SimilarDocument(
                    id=(point.payload or {}).get("document_id", point.id),
                    payload=point.payload or {},
                    dense=dense_score,
                    sparse=sparse_score,
                    score=score,
                )
            )
        result.sort(key=lambda item: item.score, reverse=True)
        return result[:limit]

    @property
    def model(self) -> SentenceTransformer:
        """Загруженная модель эмбеддингов (можно передать другому индексу)."""
        return self._model

    # --- Поиск ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        with_payload: bool | list[str] = True,
    ) -> list[models.ScoredPoint]:
        """Ищет документы по dense+sparse векторам и объединяет ранги через RRF."""
        if limit < 1:
            raise ValueError("limit must be greater than zero.")
        if not self._vocabulary:
            raise RuntimeError("Index BM25 documents before searching.")

        dense = self._encode_dense([query])[0].tolist()
        sparse = self._bm25_vector(self._tokens(query))
        response = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(query=dense, using="dense", limit=limit * 2),
                models.Prefetch(query=self._sparse_model(sparse), using="sparse", limit=limit * 2),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=with_payload,
        )
        return response.points

    def count(self) -> int:
        """Число документов в коллекции."""
        return self.client.count(self.collection_name, exact=True).count

    def close(self) -> None:
        """Закрывает подключение к Qdrant и снимает блокировку папки базы."""
        self.client.close()
