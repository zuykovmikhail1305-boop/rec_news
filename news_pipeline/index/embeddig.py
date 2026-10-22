"""Индекс новостей в Qdrant по dense-эмбеддингам.

Каждый документ хранится в коллекции с одним вектором ``dense`` —
эмбеддингом модели ``REC_NEWS_EMBEDDING_MODEL`` (по умолчанию
``deepvk/USER-bge-m3``), нормированным по L2. Похожесть двух
документов — косинус их векторов (:meth:`QdrantIndex.similarity`), поиск
похожих в коллекции — :meth:`QdrantIndex.find_similar`.

Используется дедупликацией (коллекция ``news`` — заголовок с первым
абзацем) и рекомендациями (коллекция ``news_audience`` — характеристики
аудитории).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from news_pipeline.config import EMBEDDING_MODEL, MODELS_DIR, QDRANT_PATH

EMBEDDING_MODEL_NAME = EMBEDDING_MODEL
VECTOR_NAME = "dense"


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


@lru_cache(maxsize=1)
def shared_model() -> SentenceTransformer:
    """Одна загруженная модель эмбеддингов на процесс (её используют все модули)."""
    return load_embedding_model()


def embed(texts: Sequence[str]) -> np.ndarray:
    """Нормированные dense-эмбеддинги текстов общей моделью."""
    model = shared_model()
    if not texts:
        return np.empty((0, model.get_embedding_dimension()), dtype=np.float32)
    return model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)


@dataclass(frozen=True)
class TextDocument:
    """Текст для индексации и дополнительные данные результата поиска."""

    text: str
    id: str | int | None = None
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SimilarDocument:
    """Документ из индекса и косинус его вектора с заданным."""

    id: str | int
    payload: dict[str, Any]
    score: float
    vector: np.ndarray | None = None


class QdrantIndex:
    """Индексирует тексты dense-эмбеддингами и ищет похожие в Qdrant.

    Параметр ``qdrant_path`` создаёт локальную persistent-базу Qdrant.
    Для удалённого сервера передайте ``url`` и при необходимости ``api_key``.
    Локальную базу одновременно может открыть только один процесс; если
    индекс нужен нескольким процессам сразу, используйте сервер Qdrant.
    """

    def __init__(
        self,
        collection_name: str = "news",
        *,
        qdrant_path: str | Path | None = None,
        url: str | None = None,
        api_key: str | None = None,
        embedding_model: str = EMBEDDING_MODEL_NAME,
        model_path: str | Path | None = None,
        client: QdrantClient | None = None,
        model: SentenceTransformer | None = None,
    ) -> None:
        """Создаёт подключение к Qdrant и коллекцию, если её ещё нет.

        ``client`` и ``model`` позволяют нескольким индексам (коллекциям)
        работать с одной базой Qdrant и одной загруженной моделью.
        """
        if client is not None and (url is not None or qdrant_path is not None):
            raise ValueError("Pass either client or url/qdrant_path, not both.")
        if client is not None:
            self.client = client
        elif url is not None:
            self.client = QdrantClient(url=url, api_key=api_key)
        else:
            self.client = QdrantClient(path=str(qdrant_path or QDRANT_PATH))

        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        if model is None:
            model = (
                shared_model()
                if embedding_model == EMBEDDING_MODEL_NAME and model_path is None
                else load_embedding_model(embedding_model, model_path)
            )
        self._model = model
        self._ensure_collection()

    @property
    def model(self) -> SentenceTransformer:
        """Загруженная модель эмбеддингов (можно передать другому индексу)."""
        return self._model

    def _ensure_collection(self) -> None:
        """Создаёт коллекцию Qdrant с dense-вектором (косинус, HNSW)."""
        if self.client.collection_exists(self.collection_name):
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={
                VECTOR_NAME: models.VectorParams(
                    size=self._model.get_embedding_dimension(),
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
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

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Кодирует тексты в нормированные dense-векторы (по строке на текст)."""
        if not texts:
            return np.empty((0, self._model.get_embedding_dimension()), dtype=np.float32)
        return self._model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)

    @staticmethod
    def similarity(first: np.ndarray, second: np.ndarray) -> float:
        """Косинус двух нормированных векторов."""
        return float(np.dot(first, second))

    def index(
        self,
        documents: Sequence[str | TextDocument],
        *,
        recreate: bool = False,
        dense_vectors: Sequence[np.ndarray] | np.ndarray | None = None,
    ) -> None:
        """Добавляет документы в Qdrant; точка с тем же ID перезаписывается.

        ``recreate=True`` удаляет коллекцию перед загрузкой. ``dense_vectors``
        позволяет передать уже посчитанные векторы, чтобы не кодировать
        тексты повторно.
        """
        normalised = self._normalise_documents(documents)
        if not normalised:
            return
        if recreate:
            if self.client.collection_exists(self.collection_name):
                self.client.delete_collection(self.collection_name)
            self._ensure_collection()

        if dense_vectors is None:
            dense_vectors = self.encode([document.text for document in normalised])
        points = []
        for document, vector in zip(normalised, dense_vectors):
            payload = dict(document.payload or {})
            payload.setdefault("text", document.text)
            payload.setdefault("document_id", document.id)
            points.append(
                models.PointStruct(
                    id=self._point_id(document.id),
                    vector={VECTOR_NAME: np.asarray(vector).tolist()},
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=self.collection_name, points=points, wait=True)

    def find_similar(
        self,
        vector: np.ndarray,
        *,
        limit: int = 5,
        query_filter: models.Filter | None = None,
        with_vectors: bool = False,
    ) -> list[SimilarDocument]:
        """Ищет в коллекции документы с самым большим косинусом к ``vector``.

        ``query_filter`` ограничивает кандидатов по payload (например,
        окном по времени публикации).
        """
        if self.count() == 0:
            return []
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=np.asarray(vector).tolist(),
            using=VECTOR_NAME,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=[VECTOR_NAME] if with_vectors else False,
        )
        return [
            SimilarDocument(
                id=(point.payload or {}).get("document_id", point.id),
                payload=point.payload or {},
                score=float(point.score),
                vector=np.asarray(point.vector[VECTOR_NAME], dtype=np.float32) if with_vectors and point.vector else None,
            )
            for point in response.points
        ]

    def count(self, count_filter: models.Filter | None = None) -> int:
        """Число документов в коллекции (с ``count_filter`` — подходящих под фильтр)."""
        return self.client.count(
            self.collection_name, count_filter=count_filter, exact=True
        ).count

    def ensure_payload_index(
        self, field_name: str, field_schema: models.PayloadSchemaType
    ) -> None:
        """Создаёт payload-индекс поля, если его ещё нет."""
        schema = self.client.get_collection(self.collection_name).payload_schema
        if field_name not in schema:
            self.client.create_payload_index(
                self.collection_name, field_name=field_name, field_schema=field_schema
            )

    def set_payload(self, document_id: str | int, payload: Mapping[str, Any]) -> None:
        """Дописывает поля в payload документа, не трогая векторы."""
        self.client.set_payload(
            self.collection_name,
            payload=dict(payload),
            points=[self._point_id(document_id)],
            wait=True,
        )

    def close(self) -> None:
        """Закрывает подключение к Qdrant и снимает блокировку папки базы."""
        self.client.close()
