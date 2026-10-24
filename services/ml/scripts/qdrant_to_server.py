"""Перенос векторов из локальной базы Qdrant (``data/qdrant``) в сервер Qdrant в Docker.

    docker compose up -d qdrant                                   # сервер Qdrant :6333
    python scripts/qdrant_to_server.py                            # data/qdrant/storage → REC_NEWS_QDRANT_URL
    python scripts/qdrant_to_server.py --from D:/other/qdrant/storage --url http://localhost:6333

Куда — ``--url`` или ``REC_NEWS_QDRANT_URL`` из ``.env``. Копируются все коллекции (``news``,
``news_audience``, ``analytics_chunks``) с векторами и payload, ID точек сохраняются: повторный запуск
перезаписывает те же точки, дублей не будет. Пока запущен API на локальной базе, она занята —
остановите его. Код возврата: 0 — перенесено; 1 — ошибка.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def copy_collections(local_path: Path, url: str | None = None) -> bool:
    """Все коллекции локальной базы → сервер; печатает, сколько точек перенесено."""
    from qdrant_client import QdrantClient, models

    from news_pipeline.config import QDRANT_API_KEY, QDRANT_URL

    url = url or QDRANT_URL
    if not url:
        print("   ✗ не задан REC_NEWS_QDRANT_URL — куда переносить векторы? (или --url)")
        return False
    if not local_path.exists():
        print(f"   – локальной базы нет: {local_path}")
        return True
    try:
        local = QdrantClient(path=str(local_path))
    except Exception as error:  # noqa: BLE001
        print(f"   ✗ локальная база Qdrant занята (запущен API?): {error}")
        return False
    try:
        server = QdrantClient(url=url, api_key=QDRANT_API_KEY)
        existing = {item.name for item in server.get_collections().collections}
    except Exception as error:  # noqa: BLE001
        print(f"   ✗ сервер Qdrant {url} недоступен (docker compose up -d qdrant): {error}")
        local.close()
        return False
    for collection in local.get_collections().collections:
        name = collection.name
        params = local.get_collection(name).config.params
        if name not in existing:
            server.create_collection(name, vectors_config=params.vectors, sparse_vectors_config=params.sparse_vectors)
        offset, copied = None, 0
        while True:
            points, offset = local.scroll(name, limit=256, offset=offset, with_payload=True, with_vectors=True)
            if points:
                server.upsert(name, points=[models.PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                                            for point in points], wait=True)
                copied += len(points)
            if offset is None:
                break
        print(f"   ✓ {name}: {copied} точек (на сервере {server.count(name).count})")
    local.close()
    server.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="source", type=Path, default=ROOT / "data" / "qdrant" / "storage",
                        help="локальная база Qdrant (по умолчанию data/qdrant/storage)")
    parser.add_argument("--url", help="сервер Qdrant (по умолчанию REC_NEWS_QDRANT_URL)")
    args = parser.parse_args()
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"Векторы Qdrant: {args.source} → сервер")
    return 0 if copy_collections(args.source, args.url) else 1


if __name__ == "__main__":
    sys.exit(main())
