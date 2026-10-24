"""Выгрузка контракта API в ``docs/openapi.json``.

    python scripts/export_openapi.py

Схема строится из кода (FastAPI + pydantic), сервис для этого не запускается.
Запускайте после любого изменения ручек или моделей — файл служит контрактом для бэкенда.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_pipeline.api import app  # noqa: E402

OUTPUT = ROOT / "docs" / "openapi.json"


def main() -> None:
    OUTPUT.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"контракт API записан в {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
