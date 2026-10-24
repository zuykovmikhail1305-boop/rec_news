"""Подготовка новостей: data/news.db -> data/new_summary.db (заголовок + первый абзац).

    python scripts/prepare_news.py            # один проход
    python scripts/prepare_news.py --watch    # постоянно
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.preprocessing.news_summarizer import main  # noqa: E402

if __name__ == "__main__":
    main()
