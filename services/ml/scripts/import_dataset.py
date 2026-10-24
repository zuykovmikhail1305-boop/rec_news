"""Переносит CSV датасета в SQLite и проверяет, что данные совпадают.

    python scripts/import_dataset.py                # все таблицы
    python scripts/import_dataset.py --delete-csv   # после успешной проверки удалить CSV

ur_lica.csv → ur_lica.db (companies), payment.csv → payment.db (payments),
fiz_lica.csv → fiz_lica.db (individuals); см. news_pipeline/storage/dataset_db.py.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.storage.dataset_db import TABLES, import_csv, read_csv, read_table  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delete-csv", action="store_true", help="удалить CSV, если данные в базе совпали")
    args = parser.parse_args()
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    for spec in TABLES:
        if not spec.csv_path.exists():
            print(f"{spec.csv}: нет файла — пропуск" + (f" ({spec.database} уже есть)" if spec.path.exists() else ""))
            continue
        started = time.monotonic()
        rows = import_csv(spec)
        original, stored = read_csv(spec.csv_path), read_table(spec)
        same = (list(original.columns) == list(stored.columns) and len(original) == len(stored)
                and original.fillna("<NA>").astype(str).equals(stored.fillna("<NA>").astype(str)))
        size = spec.path.stat().st_size / 2**20
        print(f"{spec.csv} → {spec.database} ({spec.table}): {rows} строк, {size:.1f} МБ, "
              f"{time.monotonic() - started:.0f} с, данные {'совпадают' if same else 'НЕ совпадают'}")
        if not same:
            for column in original.columns:
                if column in stored.columns and str(original[column].dtype) != str(stored[column].dtype):
                    print(f"   {column}: {original[column].dtype} → {stored[column].dtype}")
        if args.delete_csv and same:
            spec.csv_path.unlink()
            print(f"   {spec.csv} удалён")
