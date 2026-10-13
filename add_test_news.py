"""Добавляет в ``news.db`` пачку новостей для проверки двухшаговой дедупликации.

Пачка проверяет оба шага ``NewsDeduplicator``:

* дубликаты внутри самой пачки (аквапарк: оригинал, почти полная
  и семантическая копии);
* дубликаты уже сохранённых в ``unique.db`` новостей (кофе, рыба,
  велопрокат, выставка);
* новые новости и продолжения, которые должны остаться уникальными.

Порядок работы:

1. запустите ``python add_test_news.py`` — новости добавятся в ``news.db``;
2. дождитесь, пока ``main.py`` обработает пачку;
3. запустите ``python add_test_news.py --check`` — скрипт сравнит
   содержимое ``unique.db`` с ожидаемым результатом.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from create_news_db import SeedNews

BASE_DIR = Path(__file__).resolve().parent

NEWS_TABLE = """
CREATE TABLE IF NOT EXISTS news (
    document_id      TEXT PRIMARY KEY,
    title            TEXT,
    first_paragraph  TEXT,
    full_text        TEXT NOT NULL,
    publication_time TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    url              TEXT,
    created_at       TEXT NOT NULL
)
"""

# key — это document_id; related_key — ID новости, копией или продолжением
# которой является запись (из этой пачки или из unique.db)
TEST_NEWS = [
    # Шаг 1: новая история и её копии внутри одной пачки
    SeedNews(
        key="news_101",
        title="В Звенигорске открылся крупнейший в регионе аквапарк",
        first_paragraph=(
            "В субботу в Звенигорске открылся аквапарк «Волна», ставший крупнейшим в регионе. "
            "Площадь комплекса составляет 22 тысячи квадратных метров, на ней разместились 14 горок, "
            "бассейн с искусственной волной и спа-зона. Аквапарк рассчитан на одновременное посещение "
            "3000 человек. Инвестиции в проект составили 6 миллиардов рублей."
        ),
        body=(
            "Комплекс построен на набережной рядом со станцией метро «Заречная». Аквапарк будет работать "
            "ежедневно с 10:00 до 22:00, взрослый билет на четыре часа стоит 1800 рублей, детский — 1200 рублей.",
        ),
        url="https://zven-city.example/city/akvapark-volna-otkrylsya",
        publication_time="2026-10-03T10:00:00",
        event_type="city",
    ),
    SeedNews(
        key="news_102",
        title="В Звенигорске открылся крупнейший в регионе аквапарк",
        first_paragraph=(
            "В субботу в Звенигорске открылся аквапарк «Волна», ставший самым крупным в регионе. "
            "Площадь комплекса составляет 22 тыс. квадратных метров, на ней разместились 14 горок, "
            "бассейн с искусственной волной и спа-зона. Аквапарк рассчитан на одновременное посещение "
            "3 тысяч человек. Инвестиции в проект составили 6 млрд рублей."
        ),
        body=(
            "Комплекс построен на набережной рядом со станцией метро \"Заречная\". Аквапарк будет работать "
            "ежедневно с 10:00 до 22:00, взрослый билет на четыре часа стоит 1800 руб., детский - 1200 руб.",
        ),
        url="https://region-news.example/2026/10/03/zvenigorsk-akvapark",
        publication_time="2026-10-03T11:20:00",
        event_type="city",
        relation="near_duplicate",
        related_key="news_101",
    ),
    SeedNews(
        key="news_103",
        title="«Волна» приняла первых гостей: в Звенигорске заработал большой аквапарк",
        first_paragraph=(
            "Первые посетители в субботу пришли в новый звенигорский аквапарк «Волна» — самый большой в регионе. "
            "На 22 тысячах квадратных метров построены 14 водных горок, бассейн с волнами и спа-зона, "
            "а единовременно комплекс может принять до трёх тысяч гостей. "
            "Инвесторы вложили в строительство 6 млрд рублей."
        ),
        body=(
            "Аквапарк находится на набережной у метро «Заречная» и открыт каждый день с 10 утра до 10 вечера. "
            "Четыре часа отдыха обойдутся взрослому в 1800 рублей, ребёнку — в 1200.",
        ),
        url="https://zven-vedomosti.example/news/2026/10/03/volna-pervye-gosti",
        publication_time="2026-10-03T15:00:00",
        event_type="city",
        relation="semantic_duplicate",
        related_key="news_101",
    ),
    SeedNews(
        key="news_104",
        title="Аквапарк «Волна» за первую неделю посетили 25 тысяч человек",
        first_paragraph=(
            "За первую неделю работы звенигорский аквапарк «Волна» посетили 25 тысяч человек. "
            "В выходные комплекс работал на пределе вместимости, и администрации пришлось ввести "
            "онлайн-бронирование сеансов. Самой популярной оказалась горка «Торнадо» высотой 18 метров."
        ),
        body=(
            "Из-за очередей на входе руководство аквапарка решило с 10 октября продлить работу до полуночи "
            "по пятницам и субботам. Также в комплексе откроют второе кафе и дополнительную кассу.",
        ),
        url="https://zven-city.example/city/akvapark-volna-nedelya",
        publication_time="2026-10-10T12:00:00",
        event_type="city",
        relation="continuation",
        related_key="news_101",
    ),
    # Шаг 2: копии новостей, которые уже лежат в unique.db
    SeedNews(
        key="news_105",
        title="Оптовые цены на кофе выросли на 30% из-за засухи",
        first_paragraph=(
            "Оптовые цены на кофе арабика за месяц выросли на 30%. "
            "Аналитики связывают подорожание с сильнейшей за десять лет засухой в крупнейших странах-производителях, "
            "из-за которой урожай может сократиться на пятую часть. "
            "Сети кофеен уже предупредили о возможном повышении цен на напитки до конца года."
        ),
        body=(
            "Сильнее всего засуха ударила по плантациям в горных районах, где осадков не было почти три месяца. "
            "Часть фермеров уже сообщила о гибели молодых деревьев, что может сказаться на урожае и в следующем сезоне.",
        ),
        url="https://news-aggregator.example/economy/kofe-30-procentov",
        publication_time="2026-10-04T09:00:00",
        event_type="economy",
        relation="near_duplicate",
        related_key="news_012",
    ),
    SeedNews(
        key="news_106",
        title="В Курило-Камчатском жёлобе нашли неизвестную науке рыбу",
        first_paragraph=(
            "Биологи сообщили об открытии ранее неизвестной рыбы, пойманной на четырёхкилометровой глубине "
            "в Курило-Камчатском жёлобе. Двадцатисантиметровая рыба без чешуи почти не имеет окраски "
            "и выглядит прозрачной. Новому виду дали название Pseudoliparis vetrovi; по мнению учёных, "
            "выдерживать огромное давление ему помогают необычные мембраны клеток."
        ),
        body=(
            "Рыб поймали автоматической ловушкой с борта судна «Академик Орлов». Камеры показали, что они "
            "держатся у дна стайками и охотятся на мелких рачков.",
        ),
        url="https://ocean-today.example/discoveries/pseudoliparis-vetrovi",
        publication_time="2026-10-04T14:00:00",
        event_type="science",
        relation="semantic_duplicate",
        related_key="news_010",
    ),
    SeedNews(
        key="news_107",
        title="Звенигорцы смогут бесплатно кататься на прокатных велосипедах",
        first_paragraph=(
            "Городские власти Звенигорска запускают бесплатный велопрокат. С первого октября по городу "
            "расставят 2000 велосипедов на 120 станциях, а полчаса катания не будут стоить ничего. "
            "Арендовать велосипед можно будет в мобильном приложении города."
        ),
        body=(
            "Велосипеды появятся у метро, в парках и в больших жилых районах, среди них будет 300 электрических. "
            "После бесплатных 30 минут минута поездки на электровелосипеде обойдётся в 5 рублей.",
        ),
        url="https://zven-vedomosti.example/news/2026/10/05/velosipedy-besplatno",
        publication_time="2026-10-05T10:00:00",
        event_type="city",
        relation="semantic_duplicate",
        related_key="news_014",
    ),
    SeedNews(
        key="news_108",
        title="В Звенигорском музее открылась выставка авангардистов",
        first_paragraph=(
            "В Звенигорском художественном музее открылась выставка «Линия и цвет», посвящённая авангарду "
            "20-х годов прошлого века. Посетители увидят более 150 работ из частных коллекций, многие из которых "
            "показывают впервые. Экспозиция продлится до конца января."
        ),
        body=(
            "Центральное место экспозиции занимают полотна художницы Веры Лучинской, которые почти век хранились "
            "в семейном архиве.",
        ),
        url="https://art-news.example/exhibitions/zvenigorsk-liniya-i-tsvet",
        publication_time="2026-10-05T16:00:00",
        event_type="culture",
        relation="near_duplicate",
        related_key="news_018",
    ),
    # Должны остаться уникальными: новая тема и продолжение новости из unique.db
    SeedNews(
        key="news_109",
        title="Банк «Северный» снизил ставки по ипотеке до 9%",
        first_paragraph=(
            "Банк «Северный» объявил о снижении ставок по ипотеке на новостройки до 9 процентов годовых. "
            "Новые условия действуют с 5 октября для заёмщиков с первоначальным взносом от 20 процентов. "
            "В банке объясняют решение снижением стоимости фондирования и ростом конкуренции на рынке."
        ),
        body=(
            "По оценкам аналитиков, вслед за «Северным» ставки могут снизить и другие крупные банки региона. "
            "Ежемесячный платёж по кредиту на 6 миллионов рублей сроком на 20 лет сократится примерно "
            "на 4 тысячи рублей.",
        ),
        url="https://business-herald.example/finance/2026/10/06/severny-ipoteka-9",
        publication_time="2026-10-06T11:00:00",
        event_type="economy",
    ),
    SeedNews(
        key="news_110",
        title="Авиакомпании полностью восстановили расписание после забастовки диспетчеров",
        first_paragraph=(
            "Авиакомпании полностью вернулись к штатному расписанию после завершившейся забастовки авиадиспетчеров. "
            "За прошедшие две недели перевозчики вывезли всех пассажиров отменённых рейсов, выполнив более "
            "200 дополнительных полётов. Министерство транспорта сообщило, что первые 80 новых диспетчеров "
            "уже приступили к обучению."
        ),
        body=(
            "Профсоюз диспетчеров заявил, что будет следить за выполнением соглашения, и не исключил новых "
            "акций протеста, если обещанное повышение зарплат задержится.",
        ),
        url="https://aero-info.example/news/2026/10/07/raspisanie-vosstanovleno",
        publication_time="2026-10-07T09:00:00",
        event_type="transport",
        relation="continuation",
        related_key="news_019",
    ),
]

TEST_IDS = {item.key for item in TEST_NEWS}


def _is_duplicate(item: SeedNews) -> bool:
    return item.relation in ("near_duplicate", "semantic_duplicate")


def _existing_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        conn.execute(NEWS_TABLE)
        return {row[0] for row in conn.execute("SELECT document_id FROM news")}


def add(news_db: Path, unique_db: Path) -> None:
    """Добавляет тестовую пачку в news.db."""
    already_unique = _existing_ids(unique_db)
    missing = sorted(
        {item.related_key for item in TEST_NEWS if item.related_key not in TEST_IDS | {None}}
        - already_unique
    )
    if missing:
        print(
            f"Внимание: в {unique_db.name} нет новостей {', '.join(missing)}. "
            f"Их копии из пачки не с чем сравнить на втором шаге."
        )
    added_before = already_unique & TEST_IDS
    if added_before:
        print(
            f"Внимание: тестовые новости {', '.join(sorted(added_before))} уже есть в {unique_db.name}, "
            f"при повторном добавлении они будут дубликатами самих себя."
        )

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(news_db) as conn:
        conn.execute(NEWS_TABLE)
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO news (document_id, title, first_paragraph, full_text, "
            "publication_time, event_type, url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.key,
                    item.title,
                    item.first_paragraph,
                    item.full_text,
                    item.publication_time,
                    item.event_type,
                    item.url,
                    created_at,
                )
                for item in TEST_NEWS
            ],
        )
        added = cursor.rowcount
    print(f"Добавлено в {news_db.name}: {added} из {len(TEST_NEWS)} новостей.")

    print("\nОжидаемый результат после обработки:")
    for item in TEST_NEWS:
        if _is_duplicate(item):
            where = "в пачке" if item.related_key in TEST_IDS else "в unique.db"
            expected = f"дубликат {item.related_key} ({where})"
        elif item.relation == "continuation":
            expected = f"уникальная, продолжение {item.related_key}"
        else:
            expected = "уникальная"
        print(f"  {item.key}: {expected} — {item.title}")
    print(
        f"\nИтого: уникальных {sum(not _is_duplicate(item) for item in TEST_NEWS)}, "
        f"дубликатов {sum(_is_duplicate(item) for item in TEST_NEWS)}."
    )


def check(news_db: Path, unique_db: Path) -> None:
    """Сравнивает содержимое unique.db с ожидаемым результатом."""
    pending = _existing_ids(news_db) & TEST_IDS
    if pending:
        print(f"Ещё не обработаны: {', '.join(sorted(pending))}. Дождитесь работы main.py.")
        return

    unique = _existing_ids(unique_db)
    errors = 0
    for item in TEST_NEWS:
        expected_unique = not _is_duplicate(item)
        actual_unique = item.key in unique
        ok = expected_unique == actual_unique
        errors += not ok
        status = "OK    " if ok else "ОШИБКА"
        expected = "уникальная" if expected_unique else f"дубликат {item.related_key}"
        actual = "уникальная" if actual_unique else "удалена как дубликат"
        print(f"{status} {item.key}: ожидалось «{expected}», получено «{actual}» — {item.title}")
    print(f"\nВерно: {len(TEST_NEWS) - errors} из {len(TEST_NEWS)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="проверить результат обработки")
    parser.add_argument("--news-db", type=Path, default=BASE_DIR / "news.db")
    parser.add_argument("--unique-db", type=Path, default=BASE_DIR / "unique.db")
    args = parser.parse_args()
    if args.check:
        check(args.news_db, args.unique_db)
    else:
        add(args.news_db, args.unique_db)


if __name__ == "__main__":
    main()
