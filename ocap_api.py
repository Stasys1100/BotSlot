import os
import re
import json
import asyncio
import datetime
from typing import Optional, Tuple
from collections import defaultdict
import aiohttp
import discord
from discord.ui import View, Select
from discord import SelectOption
from vtg_api import KYIV_TZ, VtgApiClient

OCAP_API_URL = "https://stats.replays.vtg.in.ua/api/v1"
REPLAYS_API_URL = "https://replays.vtg.in.ua/api/v1"
ANNOUNCEMENT_CHANNEL_ID = 1160843618433630228

WEEKDAYS_UA = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "П'ятниця",
    5: "Субота",
    6: "Неділя"
}

# Яскраві кольори для підсумку
COLOR_WIN  = discord.Color.green()    # #2ECC71
COLOR_LOSS = discord.Color.red()      # #E74C3C
COLOR_DRAW = discord.Color.gold()     # #F1C40F

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted_matches.json")
PENDING_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_matches.json")


class OcapApiClient:
    def __init__(self, base_url: str = OCAP_API_URL, replays_url: str = REPLAYS_API_URL):
        self.base_url = base_url.rstrip("/")
        self.replays_url = replays_url.rstrip("/")

    async def get_rotation_matches(self) -> list[dict]:
        """Отримує список усіх матчів ротації (найновіші першими)."""
        url = f"{self.base_url}/rotation"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("rotation", {}).get("matches", [])
            except Exception as e:
                print(f"[OcapApiClient] Помилка отримання ротації: {e}")
        return []

    async def get_raw_operations(self) -> list[dict]:
        """Отримує завершені операції безпосередньо з replays.vtg.in.ua одразу після завершення місії."""
        url = f"{self.replays_url}/operations"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [op for op in data if op.get("tag") == "VTG" and op.get("mission_duration", 0) >= 1500]
            except Exception as e:
                print(f"[OcapApiClient] Помилка отримання operations: {e}")
        return []

    async def get_mission_squad_stats(self, mission_id: int, target_squad: str = "28") -> dict:
        """Отримує статистику конкретного загону за місію (відвідуваність/гравці, вбивства, смерті, тімкіли)."""
        if not mission_id:
            return {"players": 0, "kills": 0, "deaths": 0, "teamKills": 0}
        url = f"{self.base_url}/missions/{mission_id}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for s in data.get("squads", []):
                            if s.get("name") == target_squad:
                                return {
                                    "players": s.get("players") or s.get("attendance", 0),
                                    "kills": s.get("kills", 0),
                                    "deaths": s.get("deaths", 0),
                                    "teamKills": s.get("teamKills", 0)
                                }
            except Exception as e:
                print(f"[OcapApiClient] Помилка отримання статистики місії {mission_id}: {e}")
        return {"players": 0, "kills": 0, "deaths": 0, "teamKills": 0}


def normalize_role_title(raw_role: str) -> str:
    """Приводить назву ролі до акуратного вигляду (Оборона / Атака / Зустрічний бій)."""
    if not raw_role:
        return ""
    r = raw_role.strip()
    if r.upper() == "ЗУСТРІЧНИЙ БІЙ":
        return "Зустрічний бій"
    return r.capitalize() if r.isupper() else r


def clean_mission_name_fallback(raw_name: str) -> str:
    """Очищує технічне ім'я файлу, якщо офіційне не знайдене в розкладі."""
    name = re.sub(r'^(?:VTG|mVTG)[_ ]*', '', raw_name, flags=re.IGNORECASE)
    name = re.sub(r'_\d+(?:_v[\d\w_]+)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'_v[\d\w_]+$', '', name, flags=re.IGNORECASE)
    name = name.replace('_', ' ').strip()
    return name or raw_name


async def fetch_official_mission_names(vtg_api_key: str, target_squad: str = "28") -> dict:
    """
    Отримує гарні офіційні назви місій та ролі загону (Оборона/Атака/Зустрічний бій)
    із розкладу уікендів порталу VTG
    (наприклад: 'Заколот', 'Busan Pocket', 'New Paralel', 'Захід-82\'').
    """
    if not vtg_api_key:
        return {}
    try:
        client = VtgApiClient(vtg_api_key)
        weekends = await client.get_published_weekends(take=15)
        cache = {}
        for wk in weekends:
            games = wk.get("games", [])
            by_date = defaultdict(list)
            for g in games:
                raw_d = g.get("date")
                if raw_d:
                    try:
                        dt = datetime.datetime.fromisoformat(raw_d.replace("Z", "+00:00")).astimezone(KYIV_TZ)
                        d_str = dt.strftime("%Y-%m-%d")
                        by_date[d_str].append(g)
                    except Exception:
                        pass
            for d_str, g_list in by_date.items():
                sorted_g = sorted(g_list, key=lambda x: x.get("position", 0))
                for idx, g in enumerate(sorted_g, 1):
                    m_name = (g.get("mission") or {}).get("name")
                    role = None
                    try:
                        _, role_name, _ = await client.detect_squad_side(g, target_squad=target_squad)
                        role = normalize_role_title(role_name)
                    except Exception:
                        pass
                    if m_name:
                        info_dict = {
                            "name": m_name.strip(),
                            "role": role
                        }
                        cache[(d_str, idx)] = info_dict
                        cache[(d_str, m_name.strip().lower())] = info_dict
        return cache
    except Exception as e:
        print(f"[fetch_official_mission_names] Помилка отримання назв з VTG API: {e}")
        return {}


def is_valid_match(m: dict) -> bool:
    """
    Перевірка матчу на валідність:
    1. Тривалість не менше 25 хвилин (1500 сек) — відсікає технічні рестарти.
    2. Переможець ротації строго 0 або 1.
    3. Перемога зафіксована однією з ігрових сторін Arma 3 (WEST, EAST, GUER, RESISTANCE).
    """
    if not isinstance(m, dict):
        return False
    if m.get("duration", 0) < 1500:
        return False
    if m.get("winner") not in (0, 1):
        return False
    if m.get("endSide") not in ("WEST", "EAST", "GUER", "RESISTANCE"):
        return False
    return True


def get_game_number(match: dict, all_matches: list[dict] = None) -> int:
    """Визначає номер гри на цю дату (1 або 2)."""
    if not all_matches:
        return 1
    match_date = match.get("date")
    same_date_matches = sorted(
        [x for x in all_matches if x.get("date") == match_date and is_valid_match(x)],
        key=lambda x: x.get("missionId", 0)
    )
    for idx, x in enumerate(same_date_matches, 1):
        if x.get("missionId") == match.get("missionId"):
            return idx
    return 1


def format_game_single_line(
    m: dict,
    all_matches: list[dict] = None,
    names_cache: dict = None,
    squad_stats: dict = None,
    target_squad: str = "28"
) -> Tuple[str, Optional[bool]]:
    """
    Формує результат однієї гри в один рядок:
    Статус, номер гри, назва місії, тривалість, вбивства, смерті, тімкіли (без карти).
    """
    game_num = get_game_number(m, all_matches) if all_matches else 1
    date_str = m.get("date", "")
    raw_name = m.get("name", "Невідома місія")
    duration_min = m.get("duration", 0) // 60

    # Офіційна назва місії або очищена технічна, та роль загону
    mission_name = None
    role_name = None
    if names_cache:
        cached = names_cache.get((date_str, game_num))
        if not cached:
            fallback_n = clean_mission_name_fallback(raw_name).lower()
            cached = names_cache.get((date_str, fallback_n))
        if isinstance(cached, dict):
            mission_name = cached.get("name")
            role_name = cached.get("role")
        elif isinstance(cached, str):
            mission_name = cached

    if not mission_name:
        mission_name = clean_mission_name_fallback(raw_name)

    winner = m.get("winner")
    # Сторона 1 містить загін 28 (індекс 0)
    if winner == 0:
        is_win = True
        marker = "🟢"
        status = f"**[{target_squad}] ПЕРЕМОГА**"
    elif winner == 1:
        is_win = False
        marker = "🔴"
        status = f"**[{target_squad}] ПОРАЗКА**"
    else:
        is_win = None
        marker = "⚪"
        status = f"**[{target_squad}] НІЧИЯ**"

    # Роль загону в місії (Оборона / Атака / Зустрічний бій)
    role_str = f" [{role_name}]" if role_name else ""

    # Загальна статистика загону за матч (відвідуваність/гравці, вбивства, смерті, тімкіли)
    stats_str = ""
    if squad_stats:
        p = squad_stats.get("players", 0)
        k = squad_stats.get("kills", 0)
        d = squad_stats.get("deaths", 0)
        tk = squad_stats.get("teamKills", 0)
        players_part = f" · Гравців: {p}" if p else ""
        stats_str = f"{players_part} · Вбивств: {k} · Смертей: {d} · ТК: {tk}"

    line = f"{marker} {status} · `Гра {game_num}` · `{mission_name}`{role_str} ({duration_min} хв){stats_str}"
    return line, is_win


async def build_single_game_embed(
    m: dict,
    client: OcapApiClient,
    all_matches: list[dict] = None,
    names_cache: dict = None,
    target_squad: str = "28"
) -> discord.Embed:
    """Формує однорядковий Embed для однієї щойно завершеної місії (авто-трекер)."""
    date_str = m.get("date", "")
    day_name = ""
    short_date = ""
    if date_str:
        try:
            dt = datetime.date.fromisoformat(date_str)
            day_name = WEEKDAYS_UA.get(dt.weekday(), "")
            short_date = dt.strftime("%d.%m")
        except Exception:
            short_date = date_str

    mid = m.get("missionId", 0)
    squad_stats = await client.get_mission_squad_stats(mid, target_squad=target_squad) if mid else {}

    line, is_win = format_game_single_line(
        m,
        all_matches=all_matches,
        names_cache=names_cache,
        squad_stats=squad_stats,
        target_squad=target_squad
    )
    color = COLOR_WIN if is_win is True else (COLOR_LOSS if is_win is False else COLOR_DRAW)

    title = f"VTG · {day_name} ({short_date})" if day_name else "VTG"
    return discord.Embed(title=title, description=line, color=color)


def build_preliminary_game_embed(
    op: dict,
    all_ops: list[dict] = None,
    names_cache: dict = None,
    target_squad: str = "28"
) -> discord.Embed:
    """
    Формує швидкий попередній Embed одразу після завершення гри на сервері,
    поки сервер статистики ще не завершив 10-хвилинний підрахунок результатів.
    """
    date_str = op.get("date", "")
    day_name = ""
    short_date = ""
    if date_str:
        try:
            dt = datetime.date.fromisoformat(date_str)
            day_name = WEEKDAYS_UA.get(dt.weekday(), "")
            short_date = dt.strftime("%d.%m")
        except Exception:
            short_date = date_str

    raw_name = op.get("mission_name", "Невідома місія")
    duration_min = op.get("mission_duration", 0) // 60

    # Визначаємо номер гри за датою
    game_num = 1
    if all_ops:
        same_date_ops = sorted(
            [x for x in all_ops if x.get("date") == date_str],
            key=lambda x: x.get("id", 0)
        )
        for idx, x in enumerate(same_date_ops, 1):
            if x.get("id") == op.get("id"):
                game_num = idx
                break

    # Назва місії та роль
    mission_name = None
    role_name = None
    if names_cache:
        cached = names_cache.get((date_str, game_num))
        if not cached:
            fallback_n = clean_mission_name_fallback(raw_name).lower()
            cached = names_cache.get((date_str, fallback_n))
        if isinstance(cached, dict):
            mission_name = cached.get("name")
            role_name = cached.get("role")
        elif isinstance(cached, str):
            mission_name = cached

    if not mission_name:
        mission_name = clean_mission_name_fallback(raw_name)

    role_str = f" [{role_name}]" if role_name else ""
    line = f"🏁 **[{target_squad}] МІСІЯ ЗАВЕРШЕНА** · `Гра {game_num}` · `{mission_name}`{role_str} ({duration_min} хв) · ⏳ *Підрахунок результатів та статистики...*"

    title = f"VTG · {day_name} ({short_date})" if day_name else "VTG"
    return discord.Embed(title=title, description=line, color=discord.Color.gold())


def get_matches_for_date(valid_matches: list[dict], date_str: str) -> list[dict]:
    """Повертає матчі за вказану дату у хронологічному порядку (Гра 1, потім Гра 2)."""
    day_matches = [m for m in valid_matches if m.get("date") == date_str]
    return sorted(day_matches, key=lambda x: x.get("missionId", 0))


async def build_day_embed(
    client: OcapApiClient,
    valid_matches: list[dict],
    date_str: str,
    names_cache: dict = None,
    target_squad: str = "28"
) -> Optional[discord.Embed]:
    """Формує єдиний компактний Embed для всіх ігор обраного дня зі статистикою загону."""
    day_matches = get_matches_for_date(valid_matches, date_str)
    if not day_matches:
        return None

    # Отримуємо статистику для ігор дня паралельно
    stats_tasks = [client.get_mission_squad_stats(m.get("missionId", 0), target_squad=target_squad) for m in day_matches]
    all_stats = await asyncio.gather(*stats_tasks)

    lines = []
    wins = 0
    losses = 0
    for m, sq_stats in zip(day_matches, all_stats):
        line, is_win = format_game_single_line(
            m,
            all_matches=valid_matches,
            names_cache=names_cache,
            squad_stats=sq_stats,
            target_squad=target_squad
        )
        lines.append(line)
        if is_win is True:
            wins += 1
        elif is_win is False:
            losses += 1

    # Колір смуги: зелений, якщо тільки перемоги; червоний, якщо тільки поразки; золотий, якщо 1-1
    if wins > 0 and losses == 0:
        color = COLOR_WIN
    elif losses > 0 and wins == 0:
        color = COLOR_LOSS
    else:
        color = COLOR_DRAW

    dt = datetime.date.fromisoformat(date_str)
    day_name = WEEKDAYS_UA.get(dt.weekday(), "")
    short_date = dt.strftime("%d.%m")

    return discord.Embed(
        title=f"VTG · {day_name} ({short_date})",
        description="\n".join(lines),
        color=color
    )


# ─── UI компоненти: Меню вибору ігрового дня ──────────────────────────────────────

class VtgResultsDateSelect(Select):
    def __init__(self, valid_matches: list[dict], caller_id: int, names_cache: dict = None, target_squad: str = "28", limit: int = 15):
        self.valid_matches = valid_matches
        self.caller_id = caller_id
        self.names_cache = names_cache or {}
        self.target_squad = target_squad

        by_date = defaultdict(list)
        for m in valid_matches:
            by_date[m["date"]].append(m)

        sorted_dates = sorted(by_date.keys(), reverse=True)[:limit]
        self.sorted_dates = sorted_dates

        options = []
        for d in sorted_dates:
            dt = datetime.date.fromisoformat(d)
            day_name = WEEKDAYS_UA.get(dt.weekday(), "")
            d_matches = sorted(by_date[d], key=lambda x: x["missionId"])
            
            label = f"{day_name}, {dt.strftime('%d.%m.%Y')}"[:100]
            
            desc_names = []
            for idx, m in enumerate(d_matches, 1):
                cached = self.names_cache.get((d, idx))
                if isinstance(cached, dict):
                    clean_n = cached.get("name") or clean_mission_name_fallback(m.get("name", ""))
                    r = cached.get("role")
                    if r:
                        desc_names.append(f"{clean_n[:14]} [{r}]")
                    else:
                        desc_names.append(clean_n[:18])
                elif isinstance(cached, str):
                    desc_names.append(cached[:18])
                else:
                    clean_n = clean_mission_name_fallback(m.get("name", ""))
                    desc_names.append(clean_n[:18])
            mission_titles = ", ".join(desc_names)
            desc = f"{len(d_matches)} гри: {mission_titles}"[:100]

            options.append(SelectOption(label=label, description=desc, value=d))

        super().__init__(
            placeholder="Оберіть ігровий день...",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, inter: discord.Interaction):
        if self.caller_id and inter.user.id != self.caller_id:
            return await inter.response.send_message("Це меню викликано іншим користувачем.", ephemeral=True)

        selected_date = self.values[0]
        client = OcapApiClient()
        embed = await build_day_embed(
            client=client,
            valid_matches=self.valid_matches,
            date_str=selected_date,
            names_cache=self.names_cache,
            target_squad=self.target_squad
        )
        if not embed:
            return await inter.response.send_message("За цей день ігор не знайдено.", ephemeral=True)

        bot = inter.client
        announcement_ch = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if announcement_ch is None:
            try:
                announcement_ch = await bot.fetch_channel(ANNOUNCEMENT_CHANNEL_ID)
            except Exception:
                announcement_ch = None

        target_channel = announcement_ch if announcement_ch else inter.channel

        if self.view:
            self.view.selected = True
            self.view.stop()

        # Відправляємо результат у канал анонсів з тегом @everyone
        await target_channel.send(content="@everyone", embed=embed)

        if inter.channel.id != target_channel.id:
            await inter.response.edit_message(content=f"Результати за обраний день відправлено в <#{target_channel.id}>.", view=None)
        else:
            await inter.message.delete()


class VtgResultsDateView(View):
    def __init__(self, valid_matches: list[dict], caller_id: int, names_cache: dict = None, target_squad: str = "28"):
        super().__init__(timeout=60)
        self.selected: bool = False
        self.message: Optional[discord.Message] = None
        self.select = VtgResultsDateSelect(valid_matches, caller_id, names_cache=names_cache, target_squad=target_squad)
        self.add_item(self.select)

    async def on_timeout(self):
        if not self.selected and self.message:
            try:
                await self.message.delete()
            except Exception:
                pass


# ─── Робота зі станом ────────────────────────────────────────────────────────────

def load_posted_matches(filepath: str = STATE_FILE) -> set[int]:
    """Завантажує множину вже опублікованих ID місій."""
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except Exception as e:
        print(f"[OcapApiClient] Помилка читання {filepath}: {e}")
        return set()


def save_posted_matches(posted_ids: set[int], filepath: str = STATE_FILE):
    """Зберігає множину опублікованих ID місій у файл."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(sorted(list(posted_ids)), f, indent=2)
    except Exception as e:
        print(f"[OcapApiClient] Помилка запису {filepath}: {e}")


def load_pending_matches(filepath: str = PENDING_STATE_FILE) -> dict:
    """Завантажує словник місій, які очікують на фінальну статистику: {str(mid): {channel_id, message_id}}."""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[OcapApiClient] Помилка читання {filepath}: {e}")
        return {}


def save_pending_matches(pending: dict, filepath: str = PENDING_STATE_FILE):
    """Зберігає очікуючі оновлення місії у файл."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2)
    except Exception as e:
        print(f"[OcapApiClient] Помилка запису {filepath}: {e}")


def init_posted_matches_if_needed(current_matches: list[dict], filepath: str = STATE_FILE) -> set[int]:
    """
    При першому запуску ініціалізує файл поточними матчами,
    щоб не спамити історією минулих тижнів.
    """
    if os.path.exists(filepath):
        return load_posted_matches(filepath)

    initial_ids = {m["missionId"] for m in current_matches if isinstance(m, dict) and "missionId" in m}
    save_posted_matches(initial_ids, filepath)
    print(f"[OcapApiClient] Ініціалізовано {len(initial_ids)} існуючих місій у {filepath}")
    return initial_ids
