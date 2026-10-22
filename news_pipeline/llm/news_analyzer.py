"""Анализ новости через LLM: краткая выжимка и шаблон целевой аудитории.

Для разработки используется локальная модель в LM Studio
(OpenAI-совместимый API). Адрес, модель и ключ задаются переменными
``REC_NEWS_LLM_*`` в ``.env``; чтобы перейти на модели хакатона, достаточно
поменять их — формат запросов тот же.

Новость анализируется один раз: результат (``summary`` для векторизации
и дедупликации, ``audience`` для подбора пользователей) нужно сохранить
и переиспользовать, а не вызывать LLM на каждого пользователя.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from news_pipeline.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_S
from news_pipeline.recommendation.audience_profile import (
    INDUSTRIES,
    AudienceProfile,
    json_schema,
    regions_in_text,
    vocabulary_prompt,
)

DEFAULT_BASE_URL = LLM_BASE_URL

SYSTEM_PROMPT = """Ты аналитик Альфа-Банка. Ты читаешь новость и готовишь данные \
для сервиса, который подбирает новости руководителям крупного бизнеса.

Верни JSON из четырёх частей.

1. summary — краткая выжимка новости на русском языке, 2–4 предложения, \
не длиннее 600 символов. Начни с главного события. Укажи, кто, что сделал, \
когда, ключевые цифры. Только факты из текста: без оценок, советов, \
прогнозов от себя и фраз вроде «в статье говорится». Сохраняй модальность: \
планы, проекты и прогнозы не превращай в свершившиеся факты («сократит» \
не равно «сократил»). Пиши нейтрально и единообразно: одна и та же новость \
из разных источников должна давать почти одинаковую выжимку.

2. is_business_relevant — false, если новость не влияет на решения бизнеса \
(спорт, культура, светская хроника, происшествия без экономических \
последствий). Тогда все остальные списки — пустые.

3. market_event — рыночное событие в конкретной отрасли, если оно есть:
- industries: отрасль, В КОТОРОЙ произошло событие (значения как в industries).
- effect: что именно изменилось у этой отрасли:
  * «предложение» — цены, объём, дефицит или сроки того, что эта отрасль \
ПРОДАЁТ. Пример: подорожал стальной прокат -> industries = \
«металлургия и металлоизделия», effect = «предложение».
  * «спрос» — закупки, заказы или бюджет этой отрасли, то есть сколько она \
ПОКУПАЕТ. Пример: сократили бюджет на строительство дорог -> industries = \
«строительство», effect = «спрос».
Для общеэкономических новостей (ставка, инфляция, налоги для всех) оба \
списка пустые.

4. audience — характеристики человека, для которого эта новость важна \
для принятия бизнес-решений, и его компании. Каждое поле — список значений \
строго из словаря ниже. Оставляй поле пустым списком, если новость не \
ограничивает эту характеристику (например, ставка ЦБ важна компаниям \
любого возраста — company_ages пустой). Не перечисляй все значения подряд: \
выбирай только те, для кого новость действительно значима.
- roles: кто в компании принимает решения по этой теме (ставки и долг — \
финансовый директор, казначей; налоги и отчётность — главный бухгалтер; \
стратегия и рынок — собственник, генеральный директор).
- industries: отрасли, в которых произошло событие или которые названы \
в новости. Для общеэкономических новостей (ключевая ставка, инфляция, \
налоги для всех) оставь пустым — не пиши «финансы и страхование» только \
потому, что новость про банк или ЦБ.
- regions_named_in_text: только регионы, прямо названные в тексте. Если \
в тексте нет названий регионов или городов России — пустой список.
- foreign_trade: [«есть»], если новость важна импортёрам или экспортёрам \
(санкции, пошлины, курсы валют, логистика через границу), иначе пустой список.
- trade_countries: страны, торговли с которыми касается новость.
- bank_products: только продукты, условия или стоимость которых меняет \
новость (ключевая ставка -> кредиты, лизинг, депозиты).

Словарь допустимых значений:
{vocabulary}"""

USER_PROMPT = "Заголовок: {title}\n\nТекст новости:\n{text}"

# Ключи JSON для LLM, которые понятнее модели, чем имена полей шаблона.
LLM_FIELD_ALIASES = {"regions": "regions_named_in_text"}

# Отрасли поставщиков и покупателей LLM не заполняет напрямую: маленькие
# модели путают направление. Они выводятся из market_event:
# шок предложения в отрасли X важен тем, у кого X — поставщик;
# шок спроса в отрасли X важен тем, у кого X — покупатель.
DERIVED_FIELDS = ("supplier_industries", "customer_industries")
EVENT_EFFECTS = {"предложение": "supplier_industries", "спрос": "customer_industries"}


class LLMError(RuntimeError):
    """Ошибка обращения к LLM или разбора её ответа."""


@dataclass(frozen=True)
class NewsAnalysis:
    """Результат анализа одной новости."""

    summary: str
    audience: AudienceProfile
    is_business_relevant: bool
    model: str

    @property
    def audience_text(self) -> str:
        """Шаблон аудитории текстом — в том же формате, что и у пользователя."""
        return self.audience.to_text()

    def to_dict(self) -> dict[str, Any]:
        """Возвращает результат как JSON-совместимый словарь для сохранения."""
        return {
            "summary": self.summary,
            "is_business_relevant": self.is_business_relevant,
            "audience": self.audience.to_dict(),
            "audience_text": self.audience_text,
            "model": self.model,
        }


class ChatClient:
    """Минимальный клиент OpenAI-совместимого Chat Completions API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        """Если ``model`` не задан, берётся первая LLM из ``/models``."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.model = model or self._first_chat_model()

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> str:
        """Отправляет диалог и возвращает текст ответа модели.

        Если передана ``schema``, ответ ограничивается JSON-схемой
        (structured output). Сервер без поддержки схем получает запрос
        без неё — тогда JSON выделяется из текста при разборе.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "news_analysis", "strict": True, "schema": schema},
            }
        try:
            response = self._request("POST", "/chat/completions", body)
        except LLMError:
            if schema is None:
                raise
            body.pop("response_format")
            response = self._request("POST", "/chat/completions", body)
        try:
            return response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError(f"Неожиданный ответ LLM: {response}") from error

    def _first_chat_model(self) -> str:
        models = self._request("GET", "/models").get("data", [])
        for item in models:
            if "embed" not in item.get("id", "").lower():
                return item["id"]
        raise LLMError(f"На сервере {self.base_url} нет загруженной LLM.")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"HTTP {error.code} от {self.base_url}{path}: {detail}") from error
        except TimeoutError as error:
            # Перегруженный сервер — повод повторить запрос, а не признак недоступности.
            raise LLMError(f"таймаут запроса к {self.base_url}{path}: {error}") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise LLMError(f"таймаут запроса к {self.base_url}{path}: {error}") from error
            raise LLMError(f"LLM-сервер {self.base_url} недоступен: {error}") from error


class NewsAnalyzer:
    """Строит выжимку новости и шаблон аудитории, для которой она важна."""

    def __init__(
        self,
        client: ChatClient | None = None,
        *,
        max_input_chars: int = 6000,
        max_retries: int = 1,
        thinking: bool = True,
        max_tokens: int | None = None,
    ) -> None:
        """Создаёт анализатор.

        ``max_input_chars`` обрезает длинные тексты под контекст модели:
        у локальной qwen3-8b в LM Studio 8192 токена, системный промпт
        занимает ~2,4 тыс., 6000 символов новости — ~1,6 тыс.

        ``thinking`` — разрешить модели рассуждать перед ответом. С ним
        qwen3-8b заметно точнее различает поставщиков и покупателей, но
        работает в 3–4 раза дольше (~15–25 с на новость против ~5 с). Для
        Qwen3 выключается командой ``/no_think`` в системном промпте.
        """
        self.client = client or ChatClient(api_key=LLM_API_KEY, model=LLM_MODEL, timeout=LLM_TIMEOUT_S)
        self.max_input_chars = max_input_chars
        self.max_retries = max_retries
        self.max_tokens = max_tokens or (3500 if thinking else 1200)
        self._system_prompt = SYSTEM_PROMPT.format(
            vocabulary=vocabulary_prompt(LLM_FIELD_ALIASES, exclude=DERIVED_FIELDS)
        )
        if not thinking and "qwen3" in self.client.model.lower():
            self._system_prompt += "\n/no_think"
        self._schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 20, "maxLength": 800},
                "is_business_relevant": {"type": "boolean"},
                "market_event": {
                    "type": "object",
                    "properties": {
                        "industries": _enum_array(INDUSTRIES),
                        "effect": _enum_array(EVENT_EFFECTS),
                    },
                    "required": ["industries", "effect"],
                    "additionalProperties": False,
                },
                "audience": json_schema(LLM_FIELD_ALIASES, exclude=DERIVED_FIELDS),
            },
            "required": ["summary", "is_business_relevant", "market_event", "audience"],
            "additionalProperties": False,
        }

    def analyze(self, text: str, title: str | None = None) -> NewsAnalysis:
        """Анализирует новость и возвращает выжимку и шаблон аудитории."""
        if not text or not text.strip():
            raise ValueError("Текст новости пустой.")
        title = (title or "").strip()
        text = text.strip()[: self.max_input_chars]
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": USER_PROMPT.format(title=title or "нет", text=text)},
        ]
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            content = self.client.complete(
                messages, schema=self._schema, max_tokens=self.max_tokens
            )
            try:
                return self._parse(content, source_text=f"{title}\n{text}")
            except LLMError as error:
                last_error = error
                messages = messages[:2] + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"Ответ не прошёл проверку: {error}. "
                        "Верни исправленный JSON строго по схеме.",
                    },
                ]
        raise LLMError(f"LLM не вернула корректный ответ: {last_error}")

    def _parse(self, content: str, source_text: str) -> NewsAnalysis:
        data = _extract_json(content)
        summary = " ".join(str(data.get("summary", "")).split())
        if len(summary) < 20:
            raise LLMError("summary пустой или слишком короткий")
        audience_data = data.get("audience")
        if not isinstance(audience_data, dict):
            raise LLMError("нет объекта audience")
        is_relevant = data.get("is_business_relevant")
        if not isinstance(is_relevant, bool):
            raise LLMError("is_business_relevant должен быть true или false")
        if not is_relevant:
            # Аудитория нерелевантной новости не используется для подбора.
            audience = AudienceProfile()
        else:
            for name, alias in LLM_FIELD_ALIASES.items():
                if alias in audience_data:
                    audience_data[name] = audience_data.pop(alias)
            event = data.get("market_event") or {}
            for effect in event.get("effect") or []:
                if effect in EVENT_EFFECTS:
                    audience_data[EVENT_EFFECTS[effect]] = event.get("industries") or []
            audience = AudienceProfile.from_mapping(audience_data)
            if not audience.roles:
                raise LLMError("audience.roles пустой: укажи хотя бы одну должность")
            # Детерминированная самопроверка: регион оставляем, только если
            # он упомянут в тексте; «ВЭД: нет» для новости не несёт смысла
            # и отсекло бы её от всех компаний с внешней торговлей.
            audience = audience.with_values(
                regions=[r for r in audience.regions if r in regions_in_text(source_text)],
                foreign_trade=[v for v in audience.foreign_trade if v != "нет"],
            )
        return NewsAnalysis(
            summary=summary,
            audience=audience,
            is_business_relevant=is_relevant,
            model=self.client.model,
        )


def _enum_array(values: Any) -> dict[str, Any]:
    """JSON-схема списка уникальных значений из словаря."""
    return {
        "type": "array",
        "items": {"type": "string", "enum": list(values)},
        "uniqueItems": True,
    }


def _extract_json(content: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа, отбрасывая блок рассуждений ``<think>``."""
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("в ответе нет JSON-объекта")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise LLMError(f"невалидный JSON: {error}") from error
    if not isinstance(data, dict):
        raise LLMError("JSON не является объектом")
    return data


if __name__ == "__main__":
    analyzer = NewsAnalyzer()
    result = analyzer.analyze(
        title="ЦБ сохранил ключевую ставку на уровне 14%",
        text=(
            "11 сентября 2026 года совет директоров Банка России принял решение "
            "сохранить ключевую ставку на уровне 14% годовых. Это первая пауза в цикле "
            "смягчения денежно-кредитной политики после десяти последовательных "
            "снижений ставки, начавшегося в июне 2025 года. Регулятор указал на "
            "сохраняющееся инфляционное давление и ужесточил риторику."
        ),
    )
    print(result.summary)
    print()
    print(result.audience_text)
