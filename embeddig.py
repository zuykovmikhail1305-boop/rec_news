"""Гибридная индексация dense- и BM25 sparse-векторов в Qdrant.

Модуль принимает уже спаршенный текст и сохраняет оба вектора под именами
``dense`` и ``sparse``. Qdrant затем объединяет результаты через RRF.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class TextDocument:
    """Текст для индексации и дополнительные данные результата поиска."""

    text: str
    id: str | int | None = None
    payload: Mapping[str, Any] | None = None


class QdrantHybridIndex:
    """Индексирует и ищет тексты через dense- и BM25 sparse-векторы.

    Параметр ``qdrant_path`` создаёт локальную persistent-базу Qdrant.
    Для удалённого сервера передайте ``url`` и при необходимости ``api_key``.
    """

    _TOKEN_RE = re.compile(r"(?u)\w+")

    def __init__(
        self,
        collection_name: str = "news",
        *,
        qdrant_path: str | Path | None = None,
        url: str | None = None,
        api_key: str | None = None,
        embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        metadata_path: str | Path | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        """Создаёт подключение к Qdrant и загружает статистику BM25."""
        if client is not None and (url is not None or qdrant_path is not None):
            raise ValueError("Pass either client or url/qdrant_path, not both.")
        if client is not None:
            self.client = client
        elif url is not None:
            self.client = QdrantClient(url=url, api_key=api_key)
        else:
            self.client = QdrantClient(path=str(qdrant_path or "qdrant_data"))

        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        self._model = SentenceTransformer(embedding_model)
        self._vocabulary: dict[str, int] = {}
        self._idf: dict[int, float] = {}
        self._document_count = 0
        self._average_document_length = 0.0
        self.metadata_path = Path(
            metadata_path or f"{collection_name}.bm25.json"
        )
        self._load_bm25_metadata()

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Разбивает текст на нормализованные слова-токены."""
        return QdrantHybridIndex._TOKEN_RE.findall(text.lower())

    def _fit_bm25(self, texts: Sequence[str]) -> list[dict[int, float]]:
        """Строит словарь и IDF, затем кодирует все документы в BM25."""
        if not texts:
            raise ValueError("At least one document is required.")

        tokenized = [self._tokens(text) for text in texts]
        terms = sorted({term for tokens in tokenized for term in tokens})
        self._vocabulary = {term: index for index, term in enumerate(terms)}
        document_frequency = Counter(
            term for tokens in tokenized for term in set(tokens)
        )
        self._document_count = len(texts)
        self._average_document_length = sum(map(len, tokenized)) / len(texts)
        self._idf = {
            self._vocabulary[term]: math.log(
                1.0
                + (self._document_count - frequency + 0.5)
                / (frequency + 0.5)
            )
            for term, frequency in document_frequency.items()
        }
        return [self._bm25_vector(tokens) for tokens in tokenized]

    def _bm25_vector(
        self,
        tokens: Iterable[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> dict[int, float]:
        """Преобразует токены одного текста в разреженный BM25-вектор."""
        counts = Counter(tokens)
        length = sum(counts.values())
        average_length = self._average_document_length or 1.0
        vector: dict[int, float] = {}
        for term, term_frequency in counts.items():
            index = self._vocabulary.get(term)
            if index is None:
                continue
            idf = self._idf.get(index)
            if idf is None:
                continue
            denominator = term_frequency + k1 * (
                1.0 - b + b * length / average_length
            )
            vector[index] = idf * (
                term_frequency * (k1 + 1.0) / denominator
            )
        return vector

    def _load_bm25_metadata(self) -> None:
        """Загружает словарь BM25 из JSON, если он уже существует."""
        if not self.metadata_path.exists():
            return
        with self.metadata_path.open("r", encoding="utf-8") as file:
            metadata = json.load(file)
        if metadata.get("embedding_model") != self.embedding_model_name:
            raise ValueError(
                "The metadata uses a different embedding model: "
                f"{metadata.get('embedding_model')!r}."
            )
        self._vocabulary = {
            term: int(index) for term, index in metadata["vocabulary"].items()
        }
        self._idf = {
            int(index): float(value) for index, value in metadata["idf"].items()
        }
        self._document_count = int(metadata["document_count"])
        self._average_document_length = float(
            metadata["average_document_length"]
        )

    def _save_bm25_metadata(self) -> None:
        """Сохраняет словарь и статистику BM25 в JSON-файл."""
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "embedding_model": self.embedding_model_name,
            "vocabulary": self._vocabulary,
            "idf": self._idf,
            "document_count": self._document_count,
            "average_document_length": self._average_document_length,
        }
        with self.metadata_path.open("w", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False, indent=2)

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

    def _point_id(self, document_id: str | int) -> str | int:
        """Возвращает допустимый для Qdrant ID, сохраняя исходный ID в payload."""
        if isinstance(document_id, int):
            return document_id
        try:
            return str(uuid.UUID(document_id))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, document_id))

    def index(
        self,
        documents: Sequence[str | TextDocument],
        *,
        recreate: bool = False,
    ) -> None:
        """Обучает BM25 и загружает документы с двумя векторами в Qdrant.

        Пересчёт BM25 меняет все разреженные векторы, поэтому для полного
        переиндексирования следует передать ``recreate=True``.
        """
        normalised = self._normalise_documents(documents)
        texts = [document.text for document in normalised]
        sparse_vectors = self._fit_bm25(texts)
        dense_vectors = self._model.encode(
            texts, convert_to_numpy=True, normalize_embeddings=True
        )

        if recreate and self.client.collection_exists(self.collection_name):
            self.client.delete_collection(self.collection_name)
        self._ensure_collection()
        points = []
        for document, dense, sparse in zip(
            normalised, dense_vectors, sparse_vectors
        ):
            payload = dict(document.payload or {})
            payload.setdefault("text", document.text)
            payload.setdefault("document_id", document.id)
            points.append(
                models.PointStruct(
                    id=self._point_id(document.id),
                    vector={
                        "dense": dense.tolist(),
                        "sparse": models.SparseVector(
                            indices=list(sparse),
                            values=list(sparse.values()),
                        ),
                    },
                    payload=payload,
                )
            )
        self.client.upsert(
            collection_name=self.collection_name, points=points, wait=True
        )
        self._save_bm25_metadata()

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

        dense = self._model.encode(
            query, convert_to_numpy=True, normalize_embeddings=True
        ).tolist()
        sparse = self._bm25_vector(self._tokens(query))
        response = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(query=dense, using="dense", limit=limit * 2),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=list(sparse), values=list(sparse.values())
                    ),
                    using="sparse",
                    limit=limit * 2,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=with_payload,
        )
        return response.points