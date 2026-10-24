"""Синтетические бизнес-новости для разработки и gold-сетов.

Генерация идёт в четыре шага:

1. **Сценарии** — LLM придумывает по ``events_per_topic`` событий на каждую
   тему таксономии: тип события, 4–6 фактов с числами, участники с правовой
   формой, спикеры с должностями, цитаты, предыстория, регион, важность,
   сдвиг даты. Плюс ``noise_events`` новостей без влияния на бизнес (спорт,
   культура, происшествия) — для проверки отбора.
2. **Статьи** — LLM пишет материал в жанре и стиле издания:

   * жанры разного объёма: молния (400–800 знаков), новость (1500–2500),
     обзор рынка (2500–4000), аналитическая статья (3500–5500);
   * стиль зависит от источника: информагентство (сухо, дателайн «МОСКВА,
     14 октября.»), деловая газета, онлайн-издание, региональное СМИ, агрегатор.
3. **Копии** — для части событий:
   * ``near_duplicate`` — перепечатка мелкими правками текста (без LLM);
   * ``semantic_duplicate`` — пересказ другим изданием (LLM);
   * ``continuation`` — развитие события с новыми фактами через 2–6 дней (LLM).
4. **Разметка** — у каждой новости в ``metadata["test"]``: связь, исходная
   новость, ключ события, тема сценария (``noise`` — не бизнес), важность, жанр.

Компании и люди вымышленные; регуляторы, биржи, регионы, страны и товары —
реальные. Названия настоящих СМИ в текстах не используются.

Ответы LLM кэшируются (``data/llm_cache.db``), поэтому прерванную генерацию
можно запустить снова — готовые шаги не повторяются. Результат —
``fixtures/synthetic_news_v2.jsonl``; загрузка в ``news.db`` — :func:`load_into_news_db`.
"""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from news_pipeline.config import FIXTURES_DIR
from news_pipeline.llm.client import LLM, LLMError, default_llm
from news_pipeline.references import TOPICS, Topic
from news_pipeline.storage.news_db import StoredNews, insert_news

OUTPUT_PATH = FIXTURES_DIR / "synthetic_news_v2.jsonl"
LEGACY_PATH = FIXTURES_DIR / "synthetic_news.jsonl"  # первый корпус: 251 новость по 900–1400 знаков
PERIOD_END = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
PERIOD_DAYS = 28

_NO_THINK = "\n/no_think"


def default_corpus() -> Path:
    """Новый корпус, если он уже сгенерирован, иначе первый."""
    return OUTPUT_PATH if OUTPUT_PATH.exists() else LEGACY_PATH


# --- издания и жанры ------------------------------------------------------------


@dataclass(frozen=True)
class Outlet:
    domain: str
    style: str
    genres: tuple[tuple[str, float], ...]  # жанр и его вероятность у издания
    dateline: bool = False


OUTLETS = (
    Outlet("interfax.example", "информационного агентства: сухо, факты и цифры, без оценок от себя",
           (("flash", 0.45), ("news", 0.55)), dateline=True),
    Outlet("tass.example", "информационного агентства: сухо, официальные источники, цитаты чиновников",
           (("flash", 0.4), ("news", 0.6)), dateline=True),
    Outlet("rbc.example", "деловой газеты: аналитично, много цифр и сравнений, мнения аналитиков",
           (("news", 0.45), ("review", 0.35), ("analysis", 0.2))),
    Outlet("kommersant.example", "деловой газеты: подробности, источники на рынке, цитаты участников",
           (("news", 0.4), ("review", 0.3), ("analysis", 0.3))),
    Outlet("vedomosti.example", "деловой газеты: спокойный аналитический тон, акцент на компаниях и рынках",
           (("news", 0.4), ("review", 0.35), ("analysis", 0.25))),
    Outlet("business-herald.example", "делового онлайн-издания: живой язык, акцент на последствиях для бизнеса",
           (("news", 0.6), ("review", 0.4))),
    Outlet("frank-media.example", "финансового онлайн-издания: банки, кредиты, деньги компаний и людей",
           (("news", 0.6), ("review", 0.4))),
    Outlet("region-news.example", "регионального издания: местные власти, предприятия и последствия для региона",
           (("flash", 0.2), ("news", 0.8)), dateline=True),
    Outlet("news-aggregator.example", "новостного агрегатора: коротко пересказывает главное, ссылается на «СМИ»",
           (("flash", 0.6), ("news", 0.4))),
)
OUTLETS_BY_DOMAIN = {outlet.domain: outlet for outlet in OUTLETS}
SOURCES = tuple(outlet.domain for outlet in OUTLETS)

GENRES = {
    "flash": {"name": "молния", "chars": (400, 800), "paragraphs": "1–2",
              "structure": "главный факт с числом в первом предложении, затем одно-два уточнения"},
    "news": {"name": "новость", "chars": (1500, 2500), "paragraphs": "4–6",
             "structure": "лид (кто, что, когда, главное число) → детали и цифры → цитата участника → "
                          "предыстория («Ранее…») → последствия для рынка"},
    "review": {"name": "обзор рынка", "chars": (2500, 4000), "paragraphs": "6–8",
               "structure": "что изменилось → цифры и сравнение с прошлым периодом → причины → мнения двух "
                            "экспертов → прогноз → что это значит для компаний отрасли"},
    "analysis": {"name": "аналитическая статья", "chars": (3500, 5500), "paragraphs": "7–10",
                 "structure": "тезис → факты → разбор причин → позиции сторон с цитатами → риски и сценарии → "
                              "вывод для бизнеса"},
}

NOISE_TOPIC = Topic(id="noise", name="общество", kind="noise",
                    description="спорт, культура, погода, происшествия, наука и городская жизнь без влияния "
                                "на экономику и бизнес")



@dataclass
class SyntheticNews:
    key: str
    title: str
    text: str
    source: str
    published_at: datetime
    relation: str = "original"
    related_to: str | None = None
    event_key: str = ""
    topic: str = ""
    importance: int = 3
    entities: list[str] = field(default_factory=list)
    genre: str = "news"
    event_type: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "text": self.text, "source": self.source,
            "published_at": self.published_at.isoformat(), "relation": self.relation,
            "related_to": self.related_to, "event_key": self.event_key, "topic": self.topic,
            "importance": self.importance, "entities": self.entities, "genre": self.genre,
            "event_type": self.event_type,
        }


# --- реальные компании и люди -------------------------------------------------

# Синтетика не должна выдавать ложные события о реальных компаниях и людях.
# Основы слов (без учёта регистра): крупнейшие компании, банки, их руководители и чиновники.
REAL_NAMES = (
    "газпром", "сбер", "роснефт", "лукойл", "норникел", "норильский никел", "новатэк", "северстал", "нлмк",
    "магнитогорский металлург", "ммк", "ржд", "российские железные дороги", "аэрофлот", "втб", "газпромбанк",
    "альфа", "тинькофф", "т-банк", "мтс", "мегафон", "билайн", "ростелеком", "x5", "пятёрочк",
    "яндекс", "ozon", "wildberries", "вайлдберриз", "русал", "алроса", "татнефт",
    "сургутнефтегаз", "транснефт", "росатом", "ростех", "камаз", "автоваз", "евраз", "мечел", "фосагро",
    "акрон", "уралкали", "сибур", "интер рао", "русгидро", "россети", "совкомфлот",
    "силуанов", "набиуллин", "заботкин", "решетников", "мишустин", "путин", "орешкин", "новак", "мантуров",
    "греф", "костин", "сечин", "миллер", "алекперов", "потанин", "мордашов", "лисин", "михельсон",
    "дерипаск", "кудрин", "хуснуллин", "голикова", "белоусов", "собянин", "дюков", "гололобов",
)
# Целые слова — чтобы «сбер» не ловил «сбережения», а «евраз» — «Евразийский».
_WHOLE_WORDS = {"сбер": r"сбер(?:банк\w*)?", "евраз": r"евраз", "мтс": r"мтс", "втб": r"втб", "ммк": r"ммк",
                "x5": r"x5", "новак": r"новак\w{0,2}", "миллер": r"миллер\w{0,2}", "акрон": r"акрон\w{0,2}"}
_REAL_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(
        _WHOLE_WORDS[name] + r"(?![\w-])" if name in _WHOLE_WORDS else re.escape(name) for name in REAL_NAMES
    ) + r")",
    re.IGNORECASE,
)


def real_names(text: str) -> list[str]:
    """Реальные компании и люди из стоп-списка, упомянутые в тексте."""
    return sorted({match.group(0).lower() for match in _REAL_RE.finditer(text)})


_NAMES_RULE = """Компании и их сотрудники вымышленные: придумай названия, которых нет в реальности, \
и не используй настоящие (Газпром, Сбер, Роснефть, ЛУКОЙЛ, РЖД, Норникель, Северсталь и т. п.) и слово «Альфа» в названиях. \
Руководителей реальных ведомств (Банк России, Минфин, Минэкономразвития, ФНС, Росстат) называй только по \
должности, без имён: «глава Банка России», «министр финансов», «представитель ФНС». Регионы, страны, \
биржи, товары и индексы — реальные. Не используй названия настоящих СМИ."""


# --- шаг 1: сценарии событий --------------------------------------------------

_SCENARIO_PROMPT = """Ты выпускающий редактор деловой ленты в России. Придумай {n} разных правдоподобных \
событий по теме «{topic}» ({description}) за период с {start} по {end}.

Разнообразь типы событий: решение регулятора или правительства, статистика (Росстат, Банк России, \
Минфин, ФТС), корпоративная отчётность, сделка или инвестпроект, запуск или остановка производства, \
цены на сырьё и биржевые котировки, суд или проверка, санкции и внешняя торговля, региональное событие. \
Разный масштаб — от всей экономики до одной компании.

Для каждого события:
- title — заголовок в стиле деловой ленты, без восклицаний;
- event_type — тип события из списка выше;
- facts — 4–6 конкретных фактов: числа с единицами (млрд руб., %, т, долл./барр., п.п.), даты, \
участники; не меньше трёх чисел;
- actors — участники: организации с правовой формой (ПАО «…», АО «…», ООО «…»), ведомства и 1–2 \
человека с должностью;
- quotes — 1–2 короткие цитаты: who — кто сказал (ФИО и должность), text — цитата;
- background — 1–2 предложения: что было раньше;
- entities — организации, товары, страны, валюты из события;
- region — регион России, если событие региональное, иначе пустая строка;
- importance — масштаб для крупного бизнеса 1–5 (5 — решение ЦБ или правительства для всей \
экономики, 1 — локальная новость одной компании);
- days_ago — сколько дней назад до {end} вышла новость, 0–{max_days}.

{names_rule}
События не должны противоречить фону:
{world}
Ответ — JSON."""

_NOISE_PROMPT = """Ты редактор городской и общественной ленты в России. Придумай {n} разных правдоподобных \
новостей за период с {start} по {end} без влияния на экономику и бизнес: спорт, культура, погода, \
происшествия без экономических последствий, наука, городская жизнь.

Поля те же, что у деловых событий: title, event_type (тип новости), facts (3–5 фактов с числами), \
actors, quotes (who, text), background, entities, region, importance = 1, days_ago 0–{max_days}.
Люди и организации вымышленные, города и регионы — реальные; реальных людей не упоминай. Ответ — JSON."""

# Общий фон синтетического мира — согласован с аналитикой Альфа-Банка в texts/.
WORLD = """- ключевая ставка Банка России — 14% годовых, совет директоров сохранил её 11 сентября \
2026 года после цикла снижений; следующее заседание — 24 октября 2026 года, в октябре решений \
по ставке не было, рынок обсуждает возможное снижение;
- инфляция замедляется, но остаётся выше цели 4%; рост ВВП слабый, рынок труда перегрет, \
безработица на историческом минимуме;
- курс юаня около 11–12 рублей, доллара — около 80–90 рублей; нефть Brent — 60–70 долл./барр.;
- действуют санкции, расчёты с зарубежными партнёрами идут в основном в юанях и через \
третьи страны; растут издержки логистики через Казахстан и Китай;
- бюджет сводится с дефицитом, Минфин активно занимает на рынке ОФЗ."""

_QUOTE = {"type": "object", "properties": {"who": {"type": "string"}, "text": {"type": "string"}},
          "required": ["who", "text"]}
_SCENARIO_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "event_type": {"type": "string"},
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "quotes": {"type": "array", "items": _QUOTE},
                    "background": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "region": {"type": "string"},
                    "importance": {"type": "integer"},
                    "days_ago": {"type": "integer"},
                },
                "required": ["title", "event_type", "facts", "actors", "quotes", "background", "entities",
                             "region", "importance", "days_ago"],
            },
        }
    },
    "required": ["events"],
}


def _validate_events(data: dict[str, Any]) -> dict[str, Any]:
    """События с заголовком и фактами; события с реальными компаниями и людьми отбрасываются."""
    events, rejected = [], set()
    for event in data.get("events") or []:
        if not str(event.get("title") or "").strip() or len(event.get("facts") or []) < 3:
            continue
        found = real_names(json.dumps(event, ensure_ascii=False))
        if found:
            rejected.update(found)
            continue
        events.append(event)
    if not events:
        hint = f"; уберите реальные компании и людей: {', '.join(sorted(rejected))}" if rejected else ""
        raise LLMError("нет событий с заголовком и хотя бы тремя фактами" + hint)
    return {"events": events}


def _period(period_days: int) -> tuple[str, str]:
    start = PERIOD_END - timedelta(days=period_days - 1)
    return f"{start:%d.%m.%Y}", f"{PERIOD_END:%d.%m.%Y}"


def scenarios(topic: Topic, n: int, llm: LLM, period_days: int = PERIOD_DAYS) -> list[dict[str, Any]]:
    start, end = _period(period_days)
    if topic.kind == "noise":
        prompt = _NOISE_PROMPT.format(n=n, start=start, end=end, max_days=period_days - 1)
    else:
        prompt = _SCENARIO_PROMPT.format(n=n, topic=topic.name, description=topic.description, world=WORLD,
                                         start=start, end=end, max_days=period_days - 1, names_rule=_NAMES_RULE)
    data = llm.json(
        "synthetic_scenarios",
        [{"role": "system", "content": prompt + _NO_THINK}, {"role": "user", "content": f"Тема: {topic.name}"}],
        schema=_SCENARIO_SCHEMA, cache_key=f"v3:{topic.id}:{n}:{period_days}", validator=_validate_events,
        max_tokens=1500 + 900 * n,
    )
    return data["events"][:n]


# --- шаг 2–3: тексты ----------------------------------------------------------

_ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
    "required": ["title", "text"],
}

_ARTICLE_PROMPT = """Ты журналист {style}. Напиши материал в жанре «{genre}» на русском языке.

Объём текста: {min_chars}–{max_chars} символов, {paragraphs} абзацев, абзацы разделяй пустой строкой.
Структура: {structure}.

Правила:
- используй все факты сценария и не противоречь им; новые числа — только производные от фактов \
(разница, доля) или ориентиры из фона;
- цитаты давай с указанием, кто сказал, как в настоящих новостях;
- материал опубликован {date} ({weekday}); даты в тексте не позже неё, пиши как в новостях: \
«в понедельник», «14 октября»;
{dateline}- без подзаголовков, markdown и списков; без фраз «в статье говорится», «по данным сценария».

{names_rule}
Не противоречь фону:
{world}
Ответ — JSON с полями title (заголовок) и text (текст без заголовка)."""

_PARAPHRASE_PROMPT = """Ты журналист {style}. Перескажи новость другого издания для своей ленты: другой \
заголовок, другая структура и формулировки, но те же факты и числа, без новых фактов. Объём \
{min_chars}–{max_chars} символов, абзацы разделяй пустой строкой. {dateline}
{names_rule}
Ответ — JSON с полями title и text."""

_CONTINUATION_PROMPT = """Ты журналист {style}. Напиши продолжение сюжета: что произошло через {days} дн. \
после этой новости. Нужны новые факты и новые числа (развитие событий, реакция рынка и регуляторов, \
решения участников, новые цитаты), а не пересказ; в одном абзаце кратко напомни предысторию. Объём \
{min_chars}–{max_chars} символов, абзацы разделяй пустой строкой. Материал опубликован {date}. \
{dateline}
{names_rule}
Развитие не должно противоречить фону:
{world}
Ответ — JSON с полями title и text."""

_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
           "октября", "ноября", "декабря")


def _dateline(outlet: Outlet, published: datetime, region: str) -> str:
    """Правило дателайна для агентств и региональных изданий: «МОСКВА, 14 октября.»"""
    if not outlet.dateline:
        return ""
    date = f"{published.day} {_MONTHS[published.month - 1]}"
    city = f"главный город региона «{region}»" if region else "МОСКВА"
    return (f"- начни текст с дателайна: город заглавными буквами ({city}), дата и точка — "
            f"например «МОСКВА, {date}.»;\n")


def _validator(min_chars: int):
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        title, text = str(data.get("title") or "").strip(), str(data.get("text") or "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text.replace("\\n", "\n"))
        text = re.sub(r"^#+\s*|\*\*", "", text, flags=re.MULTILINE)
        if not title or len(text) < min_chars:
            raise LLMError(f"текст пустой или короче {min_chars} символов — напиши полный материал нужного объёма")
        found = real_names(f"{title}\n{text}")
        if found:
            raise LLMError(f"в тексте реальные компании или люди: {', '.join(found)} — замени на вымышленные, "
                           "руководителей ведомств называй только по должности")
        # дателайн «МОСКВА, 14 октября.» отдельным абзацем — приклеиваем к первому абзацу
        text = re.sub(r"^([А-ЯЁ\- ]{3,40}, \d{1,2} [а-я]+\.)\s*\n+", r"\1 ", text)
        if "\n\n" not in text and len(text) > 900:  # одним абзацем — делим по предложениям
            parts = re.split(r"(?<=[.!?])\s+", text)
            text = "\n\n".join(" ".join(parts[i:i + 3]) for i in range(0, len(parts), 3))
        return {"title": title.strip("«»\"").strip(), "text": text}

    return validate


def _write(task: str, prompt: str, content: str, key: str, llm: LLM, max_chars: int, min_chars: int) -> dict[str, str]:
    return llm.json(
        task, [{"role": "system", "content": prompt + _NO_THINK}, {"role": "user", "content": content}],
        schema=_ARTICLE_SCHEMA, cache_key=f"v3:{key}", validator=_validator(min_chars),
        max_tokens=max_chars + 3000, retries=2,
    )


def _scenario_text(event: dict[str, Any]) -> str:
    lines = [f"Заголовок: {event['title']}", f"Тип события: {event.get('event_type') or '—'}"]
    if event.get("region"):
        lines.append(f"Регион: {event['region']}")
    lines.append("Факты:\n" + "\n".join(f"- {fact}" for fact in event["facts"]))
    if event.get("actors"):
        lines.append("Участники: " + "; ".join(str(a) for a in event["actors"]))
    quotes = [q for q in event.get("quotes") or [] if isinstance(q, dict) and q.get("text")]
    if quotes:
        lines.append("Цитаты:\n" + "\n".join(f"- {q.get('who') or 'участник'}: «{q['text']}»" for q in quotes))
    if event.get("background"):
        lines.append(f"Предыстория: {event['background']}")
    return "\n".join(lines)


def _pick_genre(outlet: Outlet, rng: random.Random, noise: bool) -> str:
    if noise:
        return rng.choice(("flash", "news"))
    genres, weights = zip(*outlet.genres)
    return rng.choices(genres, weights=weights)[0]


_NEAR_EDITS = (
    ("«", '"'), ("»", '"'), (" процентов", "%"), (" миллиардов", " млрд"), (" миллионов", " млн"),
    (" рублей", " руб."), ("сообщил", "заявил"), ("Банк России", "ЦБ"),
)


def near_copy(text: str, rng: random.Random) -> str:
    """Перепечатка: мелкие правки оформления и одна-две замены слов."""
    for old, new in _NEAR_EDITS:
        if old in text and rng.random() < 0.7:
            text = text.replace(old, new)
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 3 and rng.random() < 0.5:
        paragraphs = paragraphs[:-1]  # перепечатки часто обрезают последний абзац
    return "\n\n".join(paragraphs)


# --- сборка -------------------------------------------------------------------


def generate(
    *,
    events_per_topic: int = 6,
    noise_events: int = 15,
    period_days: int = PERIOD_DAYS,
    near_share: float = 0.2,
    paraphrase_share: float = 0.2,
    continuation_share: float = 0.2,
    workers: int = 4,
    seed: int = 42,
    llm: LLM | None = None,
    output_path: Path = OUTPUT_PATH,
    topics: tuple[Topic, ...] = TOPICS,
    log: Any = print,
) -> list[SyntheticNews]:
    """Генерирует корпус и пишет его в ``output_path`` (JSON Lines)."""
    llm = llm or default_llm()
    rng = random.Random(seed)

    jobs = [(topic, events_per_topic) for topic in topics]
    if noise_events:
        jobs.append((NOISE_TOPIC, noise_events))

    def plan_topic(job: tuple[Topic, int]) -> tuple[Topic, list[dict[str, Any]]]:
        topic, n = job
        try:
            return topic, scenarios(topic, n, llm, period_days)
        except LLMError as error:
            log(f"{topic.id}: сценарии не получились: {error}")
            return topic, []

    with ThreadPoolExecutor(workers) as pool:
        per_topic = list(pool.map(plan_topic, jobs))
    seeds = [(topic, index, event) for topic, events in per_topic for index, event in enumerate(events)]
    log(f"сценариев: {len(seeds)}")

    def article(item: tuple[Topic, int, dict[str, Any]]) -> SyntheticNews | None:
        topic, index, event = item
        key = f"{topic.id}_{index}"
        # Дата, издание и жанр зависят только от ключа события — повторный запуск даёт тот же корпус.
        local = random.Random(key)
        days = min(max(int(event.get("days_ago") or 0), 0), period_days - 1)
        published = PERIOD_END - timedelta(days=days, hours=local.randint(0, 9), minutes=local.randint(0, 59))
        outlet = OUTLETS_BY_DOMAIN["region-news.example"] if event.get("region") and local.random() < 0.5 \
            else local.choice(OUTLETS)
        genre_id = _pick_genre(outlet, local, topic.kind == "noise")
        genre = GENRES[genre_id]
        min_chars, max_chars = genre["chars"]
        prompt = _ARTICLE_PROMPT.format(
            style=outlet.style, genre=genre["name"], min_chars=min_chars, max_chars=max_chars,
            paragraphs=genre["paragraphs"], structure=genre["structure"],
            date=published.strftime("%d.%m.%Y"), weekday=_WEEKDAYS[published.weekday()],
            dateline=_dateline(outlet, published, str(event.get("region") or "")),
            names_rule=_NAMES_RULE, world=WORLD,
        )
        try:
            written = _write("synthetic_article", prompt, _scenario_text(event), key, llm,
                             max_chars, int(min_chars * 0.7))
        except LLMError as error:
            log(f"{key}: статья не получилась: {error}")
            return None
        return SyntheticNews(
            key=key, title=written["title"], text=written["text"], source=outlet.domain,
            published_at=published, event_key=key, topic=topic.name if topic.kind != "noise" else "noise",
            importance=1 if topic.kind == "noise" else min(max(int(event.get("importance") or 3), 1), 5),
            entities=[str(x) for x in event.get("entities") or []], genre=genre_id,
            event_type=str(event.get("event_type") or ""),
        )

    with ThreadPoolExecutor(workers) as pool:
        originals = [news for news in pool.map(article, seeds) if news is not None]
    log(f"оригиналов: {len(originals)}")

    plan = []
    for news in originals:
        if news.topic == "noise":
            continue
        roll = rng.random()
        if roll < near_share:
            plan.append(("near_duplicate", news))
        elif roll < near_share + paraphrase_share:
            plan.append(("semantic_duplicate", news))
        if rng.random() < continuation_share and news.published_at < PERIOD_END - timedelta(days=2):
            plan.append(("continuation", news))

    def derived(item: tuple[str, SyntheticNews]) -> SyntheticNews | None:
        relation, base = item
        key = f"{base.key}_{relation.split('_')[0]}"
        local = random.Random(key)
        outlet = local.choice([o for o in OUTLETS if o.domain != base.source])
        if relation == "near_duplicate":
            return SyntheticNews(
                key=key, title=base.title, text=near_copy(base.text, local), source=outlet.domain,
                published_at=base.published_at + timedelta(hours=local.randint(1, 6)),
                relation=relation, related_to=base.key, event_key=base.event_key, topic=base.topic,
                importance=base.importance, entities=base.entities, genre=base.genre, event_type=base.event_type,
            )
        base_chars = len(base.text)
        try:
            if relation == "semantic_duplicate":
                published = base.published_at + timedelta(hours=local.randint(2, 20))
                min_chars, max_chars = int(base_chars * 0.7), int(base_chars * 1.1) + 200
                prompt = _PARAPHRASE_PROMPT.format(style=outlet.style, min_chars=min_chars, max_chars=max_chars,
                                                   dateline=_dateline(outlet, published, "").removeprefix("- "),
                                                   names_rule=_NAMES_RULE)
                genre = base.genre
            else:
                days = local.randint(2, 6)
                published = base.published_at + timedelta(days=days, hours=local.randint(0, 8))
                min_chars, max_chars = GENRES["news"]["chars"]
                prompt = _CONTINUATION_PROMPT.format(style=outlet.style, days=days, min_chars=min_chars,
                                                     max_chars=max_chars, date=published.strftime("%d.%m.%Y"),
                                                     dateline=_dateline(outlet, published, "").removeprefix("- "),
                                                     names_rule=_NAMES_RULE, world=WORLD)
                genre = "news"
            task = "synthetic_paraphrase" if relation == "semantic_duplicate" else "synthetic_continuation"
            written = _write(task, prompt, f"{base.title}\n\n{base.text}", key, llm, max_chars, int(min_chars * 0.7))
        except LLMError as error:
            log(f"{key}: не получилось: {error}")
            return None
        return SyntheticNews(
            key=key, title=written["title"], text=written["text"], source=outlet.domain,
            published_at=min(published, PERIOD_END), relation=relation, related_to=base.key,
            event_key=base.event_key if relation != "continuation" else key, topic=base.topic,
            importance=base.importance, entities=base.entities, genre=genre, event_type=base.event_type,
        )

    with ThreadPoolExecutor(workers) as pool:
        copies = [news for news in pool.map(derived, plan) if news is not None]
    corpus = sorted(originals + copies, key=lambda news: news.published_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        for news in corpus:
            file.write(json.dumps(news.to_json(), ensure_ascii=False) + "\n")
    counts = {r: sum(n.relation == r for n in corpus) for r in ("original", "near_duplicate", "semantic_duplicate", "continuation")}
    genres = {g: sum(n.genre == g for n in corpus) for g in GENRES}
    log(f"готово: {len(corpus)} новостей {counts}, жанры {genres} -> {output_path}")
    return corpus


def read_corpus(path: Path | None = None) -> list[dict[str, Any]]:
    with (path or default_corpus()).open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def load_into_news_db(news_db: Path, path: Path | None = None) -> int:
    """Загружает корпус в ``news.db`` в формате входящих новостей (эталон — в ``metadata.test``)."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = [
        StoredNews(
            document_id=f"syn_{row['key']}",
            text=row["text"],
            metadata={
                "title": row["title"],
                "url": f"https://{row['source']}/news/{row['key']}",
                "source": row["source"],
                "publication_time": row["published_at"],
                "created_at": created_at,
                "test": {
                    "relation": row["relation"],
                    "related_to": f"syn_{row['related_to']}" if row["related_to"] else None,
                    "event_key": row["event_key"],
                    "topic": row["topic"],
                    "importance": row["importance"],
                    "genre": row.get("genre"),
                    "event_type": row.get("event_type"),
                },
            },
        )
        for row in read_corpus(path)
    ]
    return insert_news(news_db, items, ignore_existing=True)
