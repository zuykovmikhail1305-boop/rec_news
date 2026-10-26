"""Разовое распознавание PDF аналитики (сканы без текстового слоя) в текст.

    python scripts/ocr_analytics.py                 # все PDF из texts/
    python scripts/ocr_analytics.py --only Gold     # файлы, в имени которых есть «Gold»

Для каждой страницы берётся встроенная картинка, EasyOCR (ru + en)
распознаёт её абзацами. Результат — ``fixtures/analytics/<имя PDF>.txt``,
страницы разделены строкой ``=== стр. N ===``. Если у PDF есть текстовый
слой, OCR не нужен — текст берётся из него. Готовые файлы не пересчитываются.
"""

import argparse
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.config import FIXTURES_DIR, OCR_GPU, OCR_LANGUAGES, ROOT_DIR  # noqa: E402

TEXTS_DIR = ROOT_DIR / "texts"
OUT_DIR = FIXTURES_DIR / "analytics"


def pdf_pages(path: Path):
    """Страницы PDF: (текстовый слой, картинки страницы)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        images = [image.data for image in page.images] if len(text) < 50 else []
        yield text, images


def recognize(only: str | None = None, force: bool = False) -> list[Path]:
    """PDF из ``texts/`` → ``fixtures/analytics/<имя>.txt``; возвращает файлы, распознанные сейчас."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reader = None
    done = []
    for pdf in sorted(TEXTS_DIR.glob("*.pdf")):
        if only and only not in pdf.name:
            continue
        target = OUT_DIR / f"{pdf.stem}.txt"
        if target.exists() and not force:
            print(f"{pdf.name}: уже распознан")
            continue
        started = time.monotonic()
        pages = []
        for number, (text, images) in enumerate(pdf_pages(pdf), 1):
            if images:
                if reader is None:
                    import easyocr

                    reader = easyocr.Reader(OCR_LANGUAGES, gpu=OCR_GPU, verbose=False)
                parts = []
                for data in images:
                    from PIL import Image
                    import numpy as np

                    image = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
                    parts.extend(reader.readtext(image, detail=0, paragraph=True))
                text = "\n\n".join(part.strip() for part in parts if part.strip())
            pages.append(f"=== стр. {number} ===\n{text}")
        target.write_text("\n\n".join(pages), encoding="utf-8")
        done.append(target)
        print(f"{pdf.name}: {len(pages)} стр., {sum(map(len, pages))} симв., {time.monotonic() - started:.0f} с", flush=True)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help="подстрока имени файла")
    parser.add_argument("--force", action="store_true", help="пересчитать готовые файлы")
    args = parser.parse_args()
    recognize(args.only, args.force)


if __name__ == "__main__":
    main()
