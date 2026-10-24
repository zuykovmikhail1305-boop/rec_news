"""Рекомендации новостей по характеристике пользователя.

Характеристика пользователя и характеристики новостей описываются одним
шаблоном :class:`audience_profile.AudienceProfile`. Для каждой уникальной
новости дедупликация (``drop_dublicate.py``) кладёт её характеристику
в коллекцию ``news_audience`` общей базы Qdrant — тем же
:class:`embeddig.QdrantIndex`, что и заголовки с первыми абзацами
в коллекции ``news``. Пока характеристики не заполняются (LLM отключена),
коллекция ``news_audience`` пуста и рекомендовать нечего.

Рекомендация:

1. характеристика пользователя (профиль или текст шаблона) векторизуется —
   dense-эмбеддинг заполненных полей шаблона;
2. в ``news_audience`` ищутся новости с самыми похожими характеристиками;
   похожесть — косинус dense-эмбеддингов;
3. к каждой новости прикладываются совпавшие поля шаблона — чтобы объяснить
   пользователю, почему новость ему подобрана.

Запуск для компании из датасета:

    python scripts/recommend.py --company U_26030 --role "финансовый директор"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

from news_pipeline.config import QDRANT_PATH
from news_pipeline.storage.dataset_db import COMPANIES, PAYMENTS, exists, read_table
from news_pipeline.recommendation.audience_profile import ROLES, AudienceProfile, counterparty_industries
from news_pipeline.index.embeddig import QdrantIndex, TextDocument
from news_pipeline.storage.news_db import ProcessedNews

AUDIENCE_COLLECTION = "news_audience"


def news_audience(news: ProcessedNews) -> AudienceProfile:
    """Характеристика новости: из ``metadata.analysis.audience`` или из текста шаблона."""
    audience = (news.metadata.get("analysis") or {}).get("audience")
    if audience:
        return AudienceProfile.from_mapping(audience)
    return AudienceProfile.from_text(news.audience_text)


def audience_document(news: ProcessedNews) -> TextDocument | None:
    """Документ для коллекции ``news_audience``; ``None``, если рекомендовать нечего.

    Не попадают новости, которые LLM признала не бизнесовыми, и новости
    с пустой характеристикой.
    """
    if (news.metadata.get("analysis") or {}).get("is_business_relevant") is False:
        return None
    profile = news_audience(news)
    if profile.is_empty():
        return None
    return TextDocument(
        text=profile.to_embedding_text(),
        id=news.document_id,
        payload={
            "document_id": news.document_id,
            "title": news.title,
            "url": news.url,
            "publication_time": news.publication_time,
            "first_paragraph": news.first_paragraph,
            "audience": profile.to_dict(),
        },
    )


def open_audience_index(
    *,
    qdrant_path: str | Path = QDRANT_PATH,
    client: QdrantClient | None = None,
    model: SentenceTransformer | None = None,
) -> QdrantIndex:
    """Открывает коллекцию характеристик новостей в общей базе Qdrant.

    ``client`` и ``model`` передаются, когда в том же процессе уже открыт
    индекс новостей (коллекция ``news``): локальную базу Qdrant можно открыть только одним клиентом.
    """
    location = {"client": client} if client is not None else {"qdrant_path": qdrant_path}
    return QdrantIndex(AUDIENCE_COLLECTION, **location, model=model)


@dataclass(frozen=True)
class Recommendation:
    """Новость, подобранная пользователю."""

    document_id: str
    title: str | None
    url: str | None
    publication_time: str | None
    first_paragraph: str | None
    score: float
    matched: dict[str, tuple[str, ...]] = field(default_factory=dict)
    audience: dict[str, list[str]] = field(default_factory=dict)


class NewsRecommender:
    """Подбирает новости по векторной похожести характеристик."""

    def __init__(
        self,
        index: QdrantIndex | None = None,
        *,
        qdrant_path: str | Path = QDRANT_PATH,
    ) -> None:
        """Подключается к коллекции ``news_audience``.

        Если ``index`` не передан, открывает локальную базу ``qdrant_path``.
        Пока её держит другой процесс (например, ``scripts/run_dedup.py``), открыть её
        нельзя — тогда передайте готовый индекс или используйте сервер Qdrant.
        """
        self.index = index or open_audience_index(qdrant_path=qdrant_path)

    def recommend(
        self,
        user: AudienceProfile | str,
        *,
        limit: int = 10,
        exclude_ids: Iterable[str] = (),
        candidates: int | None = None,
    ) -> list[Recommendation]:
        """Возвращает до ``limit`` новостей, самых подходящих пользователю.

        ``user`` — профиль пользователя или его шаблон текстом
        (:meth:`AudienceProfile.to_text`). ``exclude_ids`` — уже показанные
        новости. ``candidates`` — сколько кандидатов достать из Qdrant
        до отбрасывания исключённых (по умолчанию ``5 * limit``, не меньше 50).
        """
        profile = user if isinstance(user, AudienceProfile) else AudienceProfile.from_text(user)
        text = profile.to_embedding_text()
        if not text:
            raise ValueError("Характеристика пользователя пустая: не заполнено ни одно поле шаблона.")
        excluded = set(exclude_ids)
        pool = candidates or max(50, 5 * limit)
        similar = self.index.find_similar(self.index.encode([text])[0], limit=pool)

        result = []
        for item in similar:
            document_id = str(item.id)
            if document_id in excluded:
                continue
            audience = item.payload.get("audience") or {}
            result.append(
                Recommendation(
                    document_id=document_id,
                    title=item.payload.get("title"),
                    url=item.payload.get("url"),
                    publication_time=item.payload.get("publication_time"),
                    first_paragraph=item.payload.get("first_paragraph"),
                    score=item.score,
                    matched=profile.matches(AudienceProfile.from_mapping(audience)),
                    audience=audience,
                )
            )
            if len(result) >= limit:
                break
        return result

    def close(self) -> None:
        """Закрывает подключение к Qdrant."""
        self.index.close()


def profile_for_company(
    company_id: str,
    role: str,
    *,
    companies: Any,
    payments: Any | None = None,
) -> AudienceProfile:
    """Характеристика пользователя по компании из ``dataset/ur_lica.db`` и должности.

    ``companies`` и ``payments`` — ``pandas.DataFrame``; если переданы платежи,
    в профиль попадут отрасли поставщиков и покупателей компании.
    """
    rows = companies[companies["SYNTHETIC_U"] == company_id]
    if rows.empty:
        raise KeyError(f"Компания {company_id} не найдена.")
    suppliers: Sequence[str] = ()
    customers: Sequence[str] = ()
    if payments is not None:
        suppliers, customers = counterparty_industries(company_id, payments, companies)
    return AudienceProfile.from_company(
        rows.iloc[0], role=role, supplier_industries=suppliers, customer_industries=customers
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--company", help="SYNTHETIC_U компании из dataset/ur_lica.db")
    source.add_argument("--profile-file", type=Path, help="файл с шаблоном характеристики пользователя")
    parser.add_argument("--role", default="генеральный директор", choices=ROLES)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--qdrant-path", type=Path, default=QDRANT_PATH)
    args = parser.parse_args()

    if args.company:
        payments = read_table(PAYMENTS) if exists(PAYMENTS) else None
        profile = profile_for_company(args.company, args.role, companies=read_table(COMPANIES), payments=payments)
    else:
        profile = AudienceProfile.from_text(args.profile_file.read_text(encoding="utf-8"))

    print("Характеристика пользователя:\n" + profile.to_text() + "\n")
    recommender = NewsRecommender(qdrant_path=args.qdrant_path)
    try:
        for place, item in enumerate(recommender.recommend(profile, limit=args.limit), start=1):
            print(f"{place}. [{item.score:.3f}] {item.title}  ({item.document_id})")
            for label, values in item.matched.items():
                print(f"     совпало — {label}: {'; '.join(values)}")
    finally:
        recommender.close()


if __name__ == "__main__":
    main()
