"""Добавляет вторую тестовую пачку в data/news.db и проверяет результат.

    python scripts/add_test_news.py           # добавить пачку
    python scripts/add_test_news.py --check   # сверить unique.db с ожиданием
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.test_data.add_test_news import main  # noqa: E402

if __name__ == "__main__":
    main()
