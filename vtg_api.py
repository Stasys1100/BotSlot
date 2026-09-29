import re
import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Any
try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = datetime.timezone(datetime.timedelta(hours=3))
DEFAULT_API_URL = "https://service.beta.vtg.in.ua"

class VtgApiClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_API_URL):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json"
        }

    async def get_published_weekends(self, take: int = 5) -> list[dict]:
        """Отримує список опублікованих уікендів."""
        if not self.api_key:
            raise ValueError("VTG_API_KEY не заданий. Додайте його до .env файлу.")

        url = f"{self.base_url}/api/public/weekends?published=true&take={take}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 401:
                    raise PermissionError("Невірний VTG_API_KEY (401 Unauthorized).")
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Помилка API ({resp.status}): {text[:200]}")
                data = await resp.json()
                return data.get("data", [])

    async def get_current_week_games(self) -> tuple[Optional[dict], list[dict]]:
        """
        Знаходить уікенд та ігри для поточного/найближчого тижня.
        Використовує вікно (сьогодні - 2 дні .. сьогодні + 7 днів).
        """
        weekends = await self.get_published_weekends(take=5)
        if not weekends:
            return None, []

        now = datetime.datetime.now(KYIV_TZ)
        min_date = now - datetime.timedelta(days=2)
        max_date = now + datetime.timedelta(days=7)

        selected_weekend = None
        current_games = []

        # 1. Пошук уікенду, де хоча б одна гра потрапляє у найближче вікно дат
        for wk in weekends:
            games = wk.get("games", [])
            for g in games:
                raw_date = g.get("date")
                if raw_date:
                    try:
                        g_date = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(KYIV_TZ)
                        if min_date <= g_date <= max_date:
                            selected_weekend = wk
                            current_games = games
                            break
                    except Exception:
                        pass
            if selected_weekend:
                break

        # 2. Якщо точного збігу за вікном немає, беремо перший опублікований уікенд
        if not selected_weekend and weekends:
            selected_weekend = weekends[0]
            current_games = selected_weekend.get("games", [])

        # Сортуємо ігри за position / датою
        current_games = sorted(current_games, key=lambda x: (x.get("position", 0), x.get("date", "")))
        return selected_weekend, current_games

    async def get_side_details(self, side_id: str) -> Optional[dict]:
        """Отримує детальну інформацію про сторону, включаючи список загонів (squads)."""
        if not side_id:
            return None
        url = f"{self.base_url}/api/public/sides/{side_id}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception:
                pass
        return None

    async def detect_squad_side(self, game: dict, target_squad: str = "28") -> tuple[str, str, str]:
        """
        Визначає сторону (RED/BLUE), назву ролі (Оборона/Атака/ЗУСТРІЧНИЙ БІЙ)
        та інформаційне повідомлення для загону target_squad.
        """
        att_side = game.get("attackSide") or {}
        def_side = game.get("defenseSide") or {}
        mission_obj = (game.get("mission") or {}).get("missionObjective", "")

        is_encounter = (mission_obj == "ENCOUTER_BATTLE")

        # Перевіряємо сторону оборони
        def_id = def_side.get("id")
        att_id = att_side.get("id")

        squad_found_in_defense = False
        squad_found_in_attack = False

        if def_id:
            def_data = await self.get_side_details(def_id)
            if def_data:
                for sq in def_data.get("squads", []):
                    tag = (sq.get("tag") or "").strip()
                    name = (sq.get("name") or "").strip()
                    if tag == target_squad or target_squad in name or target_squad in tag:
                        squad_found_in_defense = True
                        break

        if not squad_found_in_defense and att_id:
            att_data = await self.get_side_details(att_id)
            if att_data:
                for sq in att_data.get("squads", []):
                    tag = (sq.get("tag") or "").strip()
                    name = (sq.get("name") or "").strip()
                    if tag == target_squad or target_squad in name or target_squad in tag:
                        squad_found_in_attack = True
                        break

        # Визначаємо сторону
        if squad_found_in_defense:
            side_type = def_side.get("type", "RED")
            role_name = "ЗУСТРІЧНИЙ БІЙ" if is_encounter else "Оборона"
            info = f"Знайдено загін «{target_squad}» у стороні оборони ({def_side.get('name', 'Оборона')})"
            return side_type, role_name, info

        if squad_found_in_attack:
            side_type = att_side.get("type", "BLUE")
            role_name = "ЗУСТРІЧНИЙ БІЙ" if is_encounter else "Атака"
            info = f"Знайдено загін «{target_squad}» у стороні атаки ({att_side.get('name', 'Атака')})"
            return side_type, role_name, info

        # За замовчуванням (якщо загін не закріплений в API) - беремо Оборону
        fallback_side = def_side.get("type", "RED")
        fallback_role = "ЗУСТРІЧНИЙ БІЙ" if is_encounter else "Оборона"
        info = f"Загін «{target_squad}» не знайдено у списках сторін, обрано за замовчуванням: {fallback_role}"
        return fallback_side, fallback_role, info

    async def get_mission_slots(self, mission_id: str, version_id: str) -> dict:
        """Отримує повний JSON структури слотів для місії."""
        url = f"{self.base_url}/api/public/missions/{mission_id}/versions/{version_id}/slots"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Помилка отримання слотів ({resp.status}): {text[:200]}")
                return await resp.json()


# ─── Хелпери форматування та парсингу ──────────────────────────────────────────

def normalize_callsign(raw: str) -> str:
    """Витягує коротку назву відділення виду '1-2' або '2-1'."""
    raw = (raw or "").strip()
    m = re.search(r'(\d+)\s*[-:]\s*(\d+)', raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # Якщо позовний без тире (наприклад 'Штаб' або 'Alpha 1')
    raw = re.sub(r'^(?:Alpha|Альфа|Bravo|Браво)\s*', '', raw, flags=re.IGNORECASE).strip()
    return raw or "1-1"

def clean_unit_role_name(raw_name: str) -> str:
    """
    Очищує рядок слота від @мітки, початкового номера та техніки.
    Приклад: '1: Командир отделения @Альфа 1-2 | 27-я омсбр | БМП-3' -> 'Командир отделения'
    Приклад: '7: Санитар | MED' -> 'Санитар | MED'
    """
    raw_name = (raw_name or "").strip()
    # Обрізаємо все, що після '@'
    at_pos = raw_name.find("@")
    if at_pos != -1:
        raw_name = raw_name[:at_pos]

    # Видаляємо нумерацію на початку '1: ', '2. '
    raw_name = re.sub(r'^\d+[\.:]\s*', '', raw_name).strip()
    raw_name = raw_name.rstrip("|").strip()
    return raw_name or "Слот"

def format_forum_title(date_str: str, mission_name: str, callsigns: list[str], game_pos: int, role_str: str) -> str:
    """
    Формує назву теми для ForumChannel строго за шаблоном:
    Дата: DD.MM.YYYY | Місія: <NAME> | Відділення: 1-2, 1-3, 1-6 | 2 Оборона
    Гарантує, що довжина рядка не перевищить 100 символів (ліміт Discord).
    """
    callsigns_text = ", ".join(callsigns)
    game_role_part = f"{game_pos} {role_str}"

    base_fmt = f"Дата: {date_str} | Місія: {{mission}} | Відділення: {{callsigns}} | {game_role_part}"
    full_title = base_fmt.format(mission=mission_name, callsigns=callsigns_text)

    if len(full_title) <= 100:
        return full_title

    # Якщо довжина перевищує 100 символів - скорочуємо назву місії
    available_for_mission = 100 - len(base_fmt.format(mission="", callsigns=callsigns_text))
    if available_for_mission >= 10:
        truncated_mission = mission_name[:available_for_mission - 2].rstrip() + "…"
        full_title = base_fmt.format(mission=truncated_mission, callsigns=callsigns_text)
        if len(full_title) <= 100:
            return full_title

    # Якщо все ще задовго - скорочуємо перелік відділень
    truncated_callsigns = callsigns_text[:30].rstrip(",") + "…"
    full_title = f"Дата: {date_str} | Місія: {mission_name[:20]}… | Відділення: {truncated_callsigns} | {game_role_part}"
    return full_title[:100]

def extract_group_list(data: Any, side_type: str = "RED", role_name: str = "Оборона") -> list[dict]:
    """
    Гарантовано витягує плаский список відділень (list of group dicts)
    з будь-якої вкладеності відповіді API (/slots), чи то dict {'RED': [...]},
    чи {'defense': {'RED': [...]}}, чи прямий список [...].
    """
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    target_role_key = "defense" if "оборон" in role_name.lower() else "attack"
    sub = (
        data.get(target_role_key)
        or data.get(side_type)
        or data.get(side_type.upper())
        or data.get(side_type.lower())
    )

    if isinstance(sub, list):
        return sub
    if isinstance(sub, dict):
        for k in (side_type, side_type.upper(), side_type.lower(), "RED", "BLUE", "red", "blue"):
            if isinstance(sub.get(k), list):
                return sub[k]
        for v in sub.values():
            if isinstance(v, list):
                return v

    for k in (side_type, side_type.upper(), side_type.lower(), "RED", "BLUE", "defense", "attack"):
        val = data.get(k)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for sub_v in val.values():
                if isinstance(sub_v, list):
                    return sub_v

    for v in data.values():
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for sub_v in v.values():
                if isinstance(sub_v, list):
                    return sub_v

    return []

