"""Рекомендации новостей для компании из датасета.

    python scripts/recommend.py --company U_26030 --role "финансовый директор"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.recommendation.recommender import main  # noqa: E402

if __name__ == "__main__":
    main()
