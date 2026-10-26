"""Демо-вход: 3 пользователя (роли), у каждого — выбор одной из 3 компаний.

Компании берутся из готовых профилей ML (``source`` — профиль, из анкеты которого копируются данные компании).
Профиль «роль + компания» ML строит при первом входе (``PUT /profiles/{id}``), дальше берёт из базы.
"""

USERS = {
    "owner": {"name": "Анна Соколова", "role": "собственник бизнеса"},
    "cfo": {"name": "Дмитрий Орлов", "role": "финансовый директор"},
    "ved": {"name": "Ирина Белова", "role": "директор по ВЭД"},
}

# Ключ компании → профиль ML, из которого берутся данные компании.
COMPANIES = {
    "trade": "u_01",       # оптовая торговля топливом, металлами, химией и стройматериалами
    "build": "u_02",       # строительство
    "logistics": "u_07",   # транспорт и логистика
}

INSIGHTS_LIMIT = 10        # сколько карточек инсайтов строить пользователю


def profile_id(user_key: str, company_key: str) -> str:
    """ID профиля ML для пары «пользователь + компания»."""
    return f"demo_{user_key}_{company_key}"


def parse_profile_id(user_id: str) -> tuple[str, str] | None:
    parts = user_id.split("_")
    if len(parts) == 3 and parts[0] == "demo" and parts[1] in USERS and parts[2] in COMPANIES:
        return parts[1], parts[2]
    return None
