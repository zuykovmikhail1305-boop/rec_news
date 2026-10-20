"""Единый шаблон описания аудитории новости и пользователя сервиса.

Один и тот же ``AudienceProfile`` используется в двух местах:

* LLM заполняет его для новости — «кому подходит эта новость»;
* сервис заполняет его для пользователя — по должности и данным
  его компании из ``ur_lica.csv``.

Все поля — списки значений из закрытых словарей ниже. Текст шаблона
строится детерминированно (фиксированный порядок полей и значений),
поэтому тексты новости и пользователя можно векторизовать и сравнивать,
а структуру — сопоставлять поле за полем.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping, Sequence

NOT_SPECIFIED = "не указано"

ROLES = (
    "собственник бизнеса",
    "генеральный директор",
    "финансовый директор",
    "главный бухгалтер",
    "руководитель казначейства",
    "другой C-level руководитель",
)

# Отрасль -> префиксы кодов ОКВЭД2. Код компании относится к отрасли
# с самым длинным совпавшим префиксом (46.71 -> «46.7», а не «46»).
INDUSTRY_OKVED_PREFIXES: dict[str, tuple[str, ...]] = {
    "сельское хозяйство и рыболовство": ("01", "02", "03"),
    "добыча нефти и газа": ("06", "09.1"),
    "добыча угля, руд и прочих полезных ископаемых": ("05", "07", "08", "09.9"),
    "пищевая промышленность": ("10", "11", "12"),
    "лёгкая промышленность": ("13", "14", "15"),
    "деревообработка, бумага и полиграфия": ("16", "17", "18"),
    "нефтепереработка, химия, фармацевтика и пластмассы": ("19", "20", "21", "22"),
    "производство стройматериалов": ("23",),
    "металлургия и металлоизделия": ("24", "25"),
    "электроника и электрооборудование": ("26", "27"),
    "машиностроение и ремонт оборудования": ("28", "29", "30", "33"),
    "прочее производство": ("31", "32"),
    "электроэнергетика и теплоснабжение": ("35",),
    "водоснабжение и утилизация отходов": ("36", "37", "38", "39"),
    "строительство": ("41", "42", "43"),
    "торговля и ремонт автомобилей": ("45",),
    "оптовая торговля продовольствием и сельхозсырьём": ("46.2", "46.3"),
    "оптовая торговля потребительскими товарами": ("46.4",),
    "оптовая торговля оборудованием и техникой": ("46.5", "46.6"),
    "оптовая торговля топливом, металлами, химией и стройматериалами": ("46.7",),
    "оптовая торговля прочая": ("46",),
    "розничная торговля": ("47",),
    "транспорт и логистика": ("49", "50", "51", "52", "53"),
    "гостиницы и общепит": ("55", "56"),
    "IT, связь и медиа": ("58", "59", "60", "61", "62", "63"),
    "финансы и страхование": ("64", "65", "66"),
    "недвижимость": ("68",),
    "профессиональные, научные и технические услуги": ("69", "70", "71", "72", "73", "74", "75"),
    "аренда, лизинг и административные услуги": ("77", "78", "79", "80", "81", "82"),
    "образование, здравоохранение и социальные услуги": ("85", "86", "87", "88"),
    "госуправление, культура, спорт и прочие услуги": ("84", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99"),
}
INDUSTRIES = tuple(INDUSTRY_OKVED_PREFIXES)

REVENUES = ("до 1 млрд ₽", "1–15 млрд ₽", "15–50 млрд ₽", "более 50 млрд ₽")
_REVENUE_BOUNDS = (1e9, 15e9, 50e9)

EMPLOYEES = ("менее 15", "16–100", "101–250", "более 250")
_EMPLOYEES_FROM_DATA = {
    "Менее 15": "менее 15",
    "16-100": "16–100",
    "101-250": "101–250",
    "Более 251": "более 250",
}

COMPANY_AGES = ("0–3 года", "4–7 лет", "8–15 лет", "16–25 лет")
_COMPANY_AGE_FROM_DATA = {
    "0-3": "0–3 года",
    "4-7": "4–7 лет",
    "8-15": "8–15 лет",
    "16-25": "16–25 лет",
}

STAGES = ("рождение", "стремительный рост", "оптимизация", "бизнес в новой реальности")

OWNERSHIPS = (
    "частная российская",
    "с иностранным участием",
    "государственная или с госучастием",
    "иная",
)
_OWNERSHIP_FROM_DATA = {
    "ЧАСТНАЯ СОБСТВЕННОСТЬ": "частная российская",
    "ИНАЯ СМЕШАННАЯ РОССИЙСКАЯ СОБСТВЕННОСТЬ": "частная российская",
    "СОВМЕСТНАЯ ЧАСТНАЯ И ИНОСТРАННАЯ СОБСТВЕННОСТЬ": "с иностранным участием",
    "СОБСТВЕННОСТЬ ИНОСТРАННЫХ ГРАЖДАН И ЛИЦ БЕЗ ГРАЖДАНСТВА": "с иностранным участием",
    "СОБСТВЕННОСТЬ ИНОСТРАННЫХ ЮРИДИЧЕСКИХ ЛИЦ": "с иностранным участием",
    "СМЕШАННАЯ ИНОСТРАННАЯ СОБСТВЕННОСТЬ": "с иностранным участием",
    "ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ": "государственная или с госучастием",
    "СОБСТВЕННОСТЬ СУБЪЕКТОВ РОССИЙСКОЙ ФЕДЕРАЦИИ": "государственная или с госучастием",
    "МУНИЦИПАЛЬНАЯ СОБСТВЕННОСТЬ": "государственная или с госучастием",
    "СМЕШАННАЯ РОССИЙСКАЯ СОБСТВЕННОСТЬ С ДОЛЕЙ ФЕДЕРАЛЬНОЙ СОБСТВЕННОСТИ": "государственная или с госучастием",
    "СМЕШАННАЯ РОССИЙСКАЯ СОБСТВЕННОСТЬ С ДОЛЕЙ СОБСТВЕННОСТИ СУБЪЕКТОВ РОССИЙСКОЙ ФЕДЕРАЦИИ": "государственная или с госучастием",
    "СОБСТВЕННОСТЬ ОБЩЕСТВЕННЫХ ОБЪЕДИНЕНИЙ": "иная",
}

# Регион компании -> Москва, Санкт-Петербург или федеральный округ
# (тот же уровень, что RC_NAME у физлиц в fiz_lica.csv).
REGIONS = (
    "Москва",
    "Санкт-Петербург",
    "Центральный ФО",
    "Северо-Западный ФО",
    "Южный ФО",
    "Северо-Кавказский ФО",
    "Приволжский ФО",
    "Уральский ФО",
    "Сибирский ФО",
    "Дальневосточный ФО",
)
_REGION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Центральный ФО": (
        "белгород", "брянск", "владимир", "воронеж", "иванов", "калуж", "костром",
        "курская", "липецк", "московская", "орлов", "рязан", "смолен", "тамбов",
        "твер", "туль", "ярослав",
    ),
    "Северо-Западный ФО": (
        "архангель", "вологод", "калининград", "ленинград", "мурман", "новгород",
        "псков", "карелия", "коми", "ненецк",
    ),
    "Южный ФО": (
        "адыгея", "калмыкия", "крым", "краснодар", "астрахан", "волгоград", "ростов",
        "севастопол", "донецк", "луганск", "запорож", "херсон", "сириус",
    ),
    "Северо-Кавказский ФО": (
        "дагестан", "ингушет", "кабардино", "карачаево", "осетия", "чечен", "ставрополь",
    ),
    "Приволжский ФО": (
        "башкортостан", "марий", "мордови", "татарстан", "удмурт", "чуваш", "пермск",
        "киров", "нижегород", "оренбург", "пензен", "самар", "саратов", "ульянов",
    ),
    "Уральский ФО": ("курган", "свердлов", "тюмен", "челябин", "ханты", "ямало"),
    "Сибирский ФО": (
        "алтай", "тыва", "хакасия", "красноярск", "иркутск", "кемеров", "новосибир",
        "омская", "томская",
    ),
    "Дальневосточный ФО": (
        "бурятия", "саха", "забайкаль", "камчат", "приморск", "хабаров", "амурск",
        "магадан", "сахалин", "еврейск", "чукот",
    ),
}

# Основы слов для поиска упоминаний регионов в тексте новости: названия
# округов, Москва, Санкт-Петербург и крупные города. Неоднозначные основы
# из _REGION_KEYWORDS (например, «саха» совпадает с «сахар») сюда не входят.
_REGION_TEXT_STEMS: dict[str, tuple[str, ...]] = {
    "Москва": ("москв",),
    "Санкт-Петербург": ("петербург", "спб"),
    "Центральный ФО": (r"центральн\w* федеральн", "цфо"),
    "Северо-Западный ФО": ("северо-западн", "сзфо"),
    "Южный ФО": (r"южн\w* федеральн", "юфо", "сочи", "новороссийск"),
    "Северо-Кавказский ФО": ("северо-кавказ", "скфо"),
    "Приволжский ФО": ("приволжск", "пфо", "казан", r"нижн\w* новгород", r"уф[аеуыо]\b",
                       "перм", "тольятти", "ижевск"),
    "Уральский ФО": ("урал", "уфо", "екатеринбург", "ханты-мансийск", "ямал"),
    "Сибирский ФО": ("сибир", "сфо", r"омск\w*\b", "барнаул", "новокузнецк", "норильск"),
    "Дальневосточный ФО": ("дальневосточн", r"дальн\w* восток", "дфо", "владивосток", "якут"),
}
_AMBIGUOUS_REGION_KEYWORDS = {"саха", "коми", "алтай", "киров", "курган", "марий", "омская"}

FOREIGN_TRADE = ("есть", "нет")
_NO_FOREIGN_TRADE_VALUES = {"", "не указано", "не имеет вэд", "nan"}

TRADE_COUNTRIES = (
    "Китай и Гонконг",
    "Турция",
    "Индия",
    "ОАЭ",
    "страны ЕС",
    "Беларусь",
    "страны Центральной Азии",
    "страны Закавказья",
    "Южная Корея и Япония",
    "страны Юго-Восточной Азии",
    "США",
    "другие страны",
)
_COUNTRY_TO_GROUP: dict[str, str] = {
    "КИТАЙ": "Китай и Гонконг", "ГОНКОНГ": "Китай и Гонконг",
    "ТУРЦИЯ": "Турция", "ИНДИЯ": "Индия", "ОАЭ": "ОАЭ",
    "БЕЛАРУСЬ": "Беларусь", "США": "США",
    "ЮЖНАЯ КОРЕЯ": "Южная Корея и Япония", "ЯПОНИЯ": "Южная Корея и Япония",
    **{c: "страны Центральной Азии" for c in (
        "КАЗАХСТАН", "УЗБЕКИСТАН", "КИРГИЗИЯ", "ТАДЖИКИСТАН", "ТУРКМЕНИСТАН")},
    **{c: "страны Закавказья" for c in ("ГРУЗИЯ", "АЗЕРБАЙДЖАН", "АРМЕНИЯ", "АБХАЗИЯ")},
    **{c: "страны Юго-Восточной Азии" for c in (
        "ВЬЕТНАМ", "ТАИЛАНД", "МАЛАЙЗИЯ", "ИНДОНЕЗИЯ", "СИНГАПУР", "ФИЛИППИНЫ",
        "КАМБОДЖА", "ЛАОС", "МЬЯНМА")},
    **{c: "страны ЕС" for c in (
        "ЛИТВА", "ЛАТВИЯ", "ЭСТОНИЯ", "ПОЛЬША", "ГЕРМАНИЯ", "ИТАЛИЯ", "ЧЕХИЯ", "ИСПАНИЯ",
        "БЕЛЬГИЯ", "ФРАНЦИЯ", "НИДЕРЛАНДЫ", "АВСТРИЯ", "ФИНЛЯНДИЯ", "ВЕНГРИЯ",
        "СЛОВАКИЯ", "СЛОВЕНИЯ", "ГРЕЦИЯ", "ПОРТУГАЛИЯ", "ДАНИЯ", "ШВЕЦИЯ", "ИРЛАНДИЯ",
        "БОЛГАРИЯ", "РУМЫНИЯ", "ХОРВАТИЯ", "КИПР", "ЛЮКСЕМБУРГ", "МАЛЬТА")},
}

BANK_PRODUCTS = (
    "кредиты",
    "лизинг",
    "депозиты",
    "банковские гарантии",
    "факторинг",
    "зарплатный проект",
    "расчётно-кассовое обслуживание",
)
_PRODUCT_COLUMNS = {
    "CL_PRODUCT_LOA_MARK": "кредиты",
    "CL_PRODUCT_LEA_MARK": "лизинг",
    "CL_PRODUCT_DEP_MARK": "депозиты",
    "CL_PRODUCT_GAR_MARK": "банковские гарантии",
    "FACTORING_MARK": "факторинг",
    "CL_PRODUCT_ZPP_MARK": "зарплатный проект",
    "CL_PRODUCT_RKO_MARK": "расчётно-кассовое обслуживание",
}


@dataclass(frozen=True)
class TemplateField:
    """Поле шаблона: имя атрибута, подпись в тексте и допустимые значения."""

    name: str
    label: str
    values: tuple[str, ...]


# Порядок полей задаёт порядок строк в тексте шаблона.
TEMPLATE_FIELDS = (
    TemplateField("roles", "Должность", ROLES),
    TemplateField("industries", "Отрасль компании", INDUSTRIES),
    TemplateField("supplier_industries", "Отрасли поставщиков компании", INDUSTRIES),
    TemplateField("customer_industries", "Отрасли покупателей компании", INDUSTRIES),
    TemplateField("revenues", "Выручка компании", REVENUES),
    TemplateField("employees", "Численность сотрудников", EMPLOYEES),
    TemplateField("company_ages", "Возраст компании", COMPANY_AGES),
    TemplateField("stages", "Стадия развития компании", STAGES),
    TemplateField("ownerships", "Форма собственности", OWNERSHIPS),
    TemplateField("regions", "Регион", REGIONS),
    TemplateField("foreign_trade", "Внешнеэкономическая деятельность", FOREIGN_TRADE),
    TemplateField("trade_countries", "Страны ВЭД", TRADE_COUNTRIES),
    TemplateField("bank_products", "Банковские продукты", BANK_PRODUCTS),
)
_FIELDS_BY_NAME = {f.name: f for f in TEMPLATE_FIELDS}


@dataclass(frozen=True)
class AudienceProfile:
    """Характеристики человека и его компании по единому шаблону.

    Пустой список означает «не указано»: для новости — характеристика
    не важна, для пользователя — данных нет.
    """

    roles: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    supplier_industries: tuple[str, ...] = ()
    customer_industries: tuple[str, ...] = ()
    revenues: tuple[str, ...] = ()
    employees: tuple[str, ...] = ()
    company_ages: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    ownerships: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    foreign_trade: tuple[str, ...] = ()
    trade_countries: tuple[str, ...] = ()
    bank_products: tuple[str, ...] = ()
    dropped_values: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AudienceProfile":
        """Собирает профиль из словаря (например, JSON от LLM).

        Значения сверяются со словарями без учёта регистра и приводятся
        к каноническому написанию и порядку. Значения не из словаря
        отбрасываются и сохраняются в ``dropped_values`` для логов.
        """
        values: dict[str, tuple[str, ...]] = {}
        dropped: list[str] = []
        for template_field in TEMPLATE_FIELDS:
            raw = data.get(template_field.name) or []
            if isinstance(raw, str):
                raw = [raw]
            canonical = {v.casefold(): v for v in template_field.values}
            chosen = set()
            for item in raw:
                key = str(item).strip().casefold()
                if key in canonical:
                    chosen.add(canonical[key])
                elif key and key != NOT_SPECIFIED:
                    dropped.append(f"{template_field.name}={item}")
            values[template_field.name] = tuple(
                v for v in template_field.values if v in chosen
            )
        return cls(**values, dropped_values=tuple(dropped))

    @classmethod
    def from_text(cls, text: str) -> "AudienceProfile":
        """Разбирает шаблон текстом (результат :meth:`to_text`) обратно в профиль.

        Строки с неизвестной подписью пропускаются, значения сверяются
        со словарями так же, как в :meth:`from_mapping`.
        """
        names_by_label = {f.label.casefold(): f.name for f in TEMPLATE_FIELDS}
        data: dict[str, list[str]] = {}
        for line in text.splitlines():
            label, separator, values = line.partition(":")
            name = names_by_label.get(label.strip().casefold())
            if separator and name:
                data[name] = [value.strip() for value in values.split(";") if value.strip()]
        return cls.from_mapping(data)

    @classmethod
    def from_company(
        cls,
        company: Mapping[str, Any],
        *,
        role: str | None = None,
        supplier_industries: Sequence[str] = (),
        customer_industries: Sequence[str] = (),
    ) -> "AudienceProfile":
        """Описывает пользователя по строке ``ur_lica.csv`` и его должности.

        ``company`` — строка таблицы компаний (dict или ``pandas.Series``).
        Отрасли контрагентов передаются отдельно, их можно посчитать
        функцией :func:`counterparty_industries` по ``payment.csv``.
        """
        products = [
            product
            for column, product in _PRODUCT_COLUMNS.items()
            if _as_float(company.get(column)) == 1.0
        ]
        has_trade = _has_foreign_trade(company)
        return cls.from_mapping(
            {
                "roles": [role] if role else [],
                "industries": _optional(industry_by_okved(company.get("MAINOKVED_CODE"))),
                "supplier_industries": list(supplier_industries),
                "customer_industries": list(customer_industries),
                "revenues": _optional(revenue_bucket(company.get("REVENUE_COMPANY"))),
                "employees": _optional(
                    _EMPLOYEES_FROM_DATA.get(str(company.get("AMOUNT_WORKERS_FNS_GRADATION")))
                ),
                "company_ages": _optional(
                    _COMPANY_AGE_FROM_DATA.get(str(company.get("COMPANY_AGE_YEARS_GRADATION")))
                ),
                "stages": _optional(_clean_str(company.get("ECO_MARK_LABEL")).lower() or None),
                "ownerships": _optional(_OWNERSHIP_FROM_DATA.get(str(company.get("OKFS_NAME")))),
                "regions": _optional(region_bucket(company.get("REGION"))),
                "foreign_trade": ["есть" if has_trade else "нет"],
                "trade_countries": country_groups(company.get("FTS_COUNTRY_NAME")) if has_trade else [],
                "bank_products": products,
            }
        )

    def with_values(self, **fields: Sequence[str]) -> "AudienceProfile":
        """Копия профиля с заменёнными полями (значения в порядке словаря)."""
        ordered = {
            name: tuple(v for v in _FIELDS_BY_NAME[name].values if v in set(values))
            for name, values in fields.items()
        }
        return replace(self, **ordered)

    def to_text(self) -> str:
        """Возвращает шаблон текстом: одна строка на поле, фиксированный порядок."""
        lines = []
        for template_field in TEMPLATE_FIELDS:
            values = getattr(self, template_field.name)
            lines.append(f"{template_field.label}: {'; '.join(values) or NOT_SPECIFIED}")
        return "\n".join(lines)

    def to_embedding_text(self) -> str:
        """Текст для векторизации: только заполненные поля шаблона.

        Строки «не указано» одинаковы у всех профилей и только сближают
        тексты по форме, поэтому в вектор не попадают. Правило одно
        и для пользователя, и для новости.
        """
        return "\n".join(
            f"{f.label}: {'; '.join(getattr(self, f.name))}"
            for f in TEMPLATE_FIELDS
            if getattr(self, f.name)
        )

    def is_empty(self) -> bool:
        """True, если ни одно поле шаблона не заполнено."""
        return not any(getattr(self, f.name) for f in TEMPLATE_FIELDS)

    def matches(self, other: "AudienceProfile") -> dict[str, tuple[str, ...]]:
        """Совпавшие значения по полям: ``{подпись поля: общие значения}``."""
        result = {}
        for f in TEMPLATE_FIELDS:
            common = set(getattr(self, f.name)) & set(getattr(other, f.name))
            if common:
                result[f.label] = tuple(v for v in f.values if v in common)
        return result

    def to_dict(self) -> dict[str, list[str]]:
        """Возвращает поля шаблона как JSON-совместимый словарь."""
        data = asdict(self)
        data.pop("dropped_values")
        return {key: list(value) for key, value in data.items()}


def industry_by_okved(code: Any) -> str | None:
    """Отрасль шаблона по коду ОКВЭД2 (самый длинный совпавший префикс)."""
    code = _clean_str(code)
    if not code:
        return None
    best: tuple[int, str] | None = None
    for industry, prefixes in INDUSTRY_OKVED_PREFIXES.items():
        for prefix in prefixes:
            if code.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), industry)
    return best[1] if best else None


def revenue_bucket(revenue: Any) -> str | None:
    """Интервал выручки шаблона по годовой выручке в рублях."""
    value = _as_float(revenue)
    if value is None:
        return None
    for bound, label in zip(_REVENUE_BOUNDS, REVENUES):
        if value < bound:
            return label
    return REVENUES[-1]


def region_bucket(region: Any) -> str | None:
    """Москва, Санкт-Петербург или федеральный округ по названию региона."""
    name = _clean_str(region).lower()
    if not name:
        return None
    if name in ("москва", "г. москва"):
        return "Москва"
    if name in ("санкт-петербург", "г. санкт-петербург"):
        return "Санкт-Петербург"
    for district, keywords in _REGION_KEYWORDS.items():
        if any(keyword in name for keyword in keywords):
            return district
    return None


def regions_in_text(text: str) -> set[str]:
    """Значения поля «Регион», которые явно упомянуты в тексте.

    Нужна для проверки ответа LLM: регион новости остаётся в профиле,
    только если текст действительно о нём говорит.
    """
    found = set()
    for region, stems in _REGION_TEXT_STEMS.items():
        if any(re.search(r"\b" + stem, text, flags=re.IGNORECASE) for stem in stems):
            found.add(region)
    for district, keywords in _REGION_KEYWORDS.items():
        for keyword in keywords:
            if keyword not in _AMBIGUOUS_REGION_KEYWORDS and re.search(
                r"\b" + keyword, text, flags=re.IGNORECASE
            ):
                found.add(district)
                break
    return found


def country_groups(countries: Any) -> list[str]:
    """Группы стран шаблона по строке ``FTS_COUNTRY_NAME`` («КИТАЙ, ТУРЦИЯ»)."""
    text = _clean_str(countries)
    if text.lower() in _NO_FOREIGN_TRADE_VALUES:
        return []
    return [
        _COUNTRY_TO_GROUP.get(country.strip().upper(), "другие страны")
        for country in text.split(",")
        if country.strip()
    ]


def counterparty_industries(
    company_id: str,
    payments: Any,
    companies: Any,
    *,
    min_share: float = 0.1,
    top_n: int = 3,
) -> tuple[list[str], list[str]]:
    """Отрасли поставщиков и покупателей компании по ``payment.csv``.

    ``payments`` и ``companies`` — ``pandas.DataFrame`` с колонками
    ``PAYER, BENEFICIARY, PAY_AMT`` и ``SYNTHETIC_U, MAINOKVED_CODE``.
    Самоплатежи не учитываются; отрасль попадает в результат, если на неё
    приходится не меньше ``min_share`` суммы платежей с известной отраслью.
    Возвращает ``(отрасли_поставщиков, отрасли_покупателей)``.
    """
    industry = (
        companies.drop_duplicates("SYNTHETIC_U")
        .set_index("SYNTHETIC_U")["MAINOKVED_CODE"]
        .map(industry_by_okved)
    )
    external = payments[payments["PAYER"] != payments["BENEFICIARY"]]

    def top(edges: Any, counterparty_column: str) -> list[str]:
        amounts = (
            edges.assign(industry=edges[counterparty_column].map(industry))
            .dropna(subset=["industry"])
            .groupby("industry")["PAY_AMT"]
            .sum()
        )
        if amounts.empty:
            return []
        shares = (amounts / amounts.sum()).sort_values(ascending=False)
        return list(shares[shares >= min_share].index[:top_n])

    suppliers = top(external[external["PAYER"] == company_id], "BENEFICIARY")
    customers = top(external[external["BENEFICIARY"] == company_id], "PAYER")
    return suppliers, customers


def json_schema(
    aliases: Mapping[str, str] | None = None,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """JSON-схема полей шаблона для structured output LLM.

    ``aliases`` переименовывает поля для LLM (имя поля -> ключ в JSON),
    если модели понятнее другое название; ``exclude`` убирает поля,
    которые заполняются не LLM, а кодом.
    """
    fields = _llm_fields(aliases, exclude)
    return {
        "type": "object",
        "properties": {
            key: {
                "type": "array",
                "items": {"type": "string", "enum": list(f.values)},
                "uniqueItems": True,
            }
            for key, f in fields
        },
        "required": [key for key, _ in fields],
        "additionalProperties": False,
    }


def vocabulary_prompt(
    aliases: Mapping[str, str] | None = None,
    exclude: Sequence[str] = (),
) -> str:
    """Описание полей и допустимых значений для системного промпта LLM."""
    return "\n".join(
        f"- {key} ({f.label}): {'; '.join(f.values)}"
        for key, f in _llm_fields(aliases, exclude)
    )


def _llm_fields(
    aliases: Mapping[str, str] | None, exclude: Sequence[str]
) -> list[tuple[str, TemplateField]]:
    aliases = aliases or {}
    return [
        (aliases.get(f.name, f.name), f) for f in TEMPLATE_FIELDS if f.name not in exclude
    ]


def _has_foreign_trade(company: Mapping[str, Any]) -> bool:
    if _as_float(company.get("FTS_FLAG")) == 1.0:
        return True
    return bool(country_groups(company.get("FTS_COUNTRY_NAME")))


def _optional(value: str | None) -> list[str]:
    return [value] if value else []


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN -> None
