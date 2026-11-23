import os
import json
import asyncio
import datetime
from typing import Optional, Tuple
from collections import defaultdict
import aiohttp
import discord
from discord.ui import View, Select
from discord import SelectOption

OCAP_API_URL = "https://stats.replays.vtg.in.ua/api/v1"
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


class OcapApiClient:
    def __init__(self, base_url: str = OCAP_API_URL):
        self.base_url = base_url.rstrip("/")

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


def format_game_single_line(m: dict, all_matches: list[dict] = None, target_squad: str = "28") -> Tuple[str, Optional[bool]]:
    """
    Формує результат однієї гри в один рядок із кольоровим маркером.
    Повертає: (рядок, чи_перемога_наша)
    """
    game_num = get_game_number(m, all_matches) if all_matches else 1
    mission_name = m.get("name", "Невідома місія")
    world = m.get("world", "")
    duration_min = m.get("duration", 0) // 60

    winner = m.get("winner")
    # Сторона 1 містить загін 28 (індекс 0)
    if winner == 0:
        is_win = True
        marker = "🟢"
        status = f"**[{target_squad}] ПЕРЕМОГА** (Сторона 1)"
    elif winner == 1:
        is_win = False
        marker = "🔴"
        status = f"**[{target_squad}] ПОРАЗКА** (Сторона 2)"
    else:
        is_win = None
        marker = "⚪"
        status = f"**[{target_squad}] НІЧИЯ**"

    line = f"{marker} {status} · `Гра {game_num}` · `{mission_name}` · `{world}` ({duration_min} хв)"
    return line, is_win


def build_single_game_embed(m: dict, all_matches: list[dict] = None, target_squad: str = "28") -> discord.Embed:
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

    line, is_win = format_game_single_line(m, all_matches=all_matches, target_squad=target_squad)
    color = COLOR_WIN if is_win is True else (COLOR_LOSS if is_win is False else COLOR_DRAW)

    title = f"VTG · {day_name} ({short_date})" if day_name else "VTG"
    return discord.Embed(title=title, description=line, color=color)


def get_matches_for_date(valid_matches: list[dict], date_str: str) -> list[dict]:
    """Повертає матчі за вказану дату у хронологічному порядку (Гра 1, потім Гра 2)."""
    day_matches = [m for m in valid_matches if m.get("date") == date_str]
    return sorted(day_matches, key=lambda x: x.get("missionId", 0))


def build_day_embed(valid_matches: list[dict], date_str: str, target_squad: str = "28") -> Optional[discord.Embed]:
    """Формує єдиний компактний Embed для всіх ігор обраного дня (кожна гра в один рядок)."""
    day_matches = get_matches_for_date(valid_matches, date_str)
    if not day_matches:
        return None

    lines = []
    wins = 0
    losses = 0
    for m in day_matches:
        line, is_win = format_game_single_line(m, all_matches=valid_matches, target_squad=target_squad)
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
    def __init__(self, valid_matches: list[dict], caller_id: int, target_squad: str = "28", limit: int = 15):
        self.valid_matches = valid_matches
        self.caller_id = caller_id
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
            mission_titles = ", ".join([m.get("name", "")[:18] for m in d_matches])
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
        embed = build_day_embed(self.valid_matches, selected_date, target_squad=self.target_squad)
        if not embed:
            return await inter.response.send_message("За цей день ігор не знайдено.", ephemeral=True)

        # Отримуємо цільовий канал анонсів 1160843618433630228
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

        # Відправляємо результат строго в канал анонсів
        await target_channel.send(embed=embed)

        # Якщо команда викликалась в іншому каналі - підтверджуємо відправку
        if inter.channel.id != target_channel.id:
            await inter.response.edit_message(content=f"Результати за обраний день відправлено в <#{target_channel.id}>.", view=None)
        else:
            await inter.message.delete()


class VtgResultsDateView(View):
    def __init__(self, valid_matches: list[dict], caller_id: int, target_squad: str = "28"):
        super().__init__(timeout=60)
        self.selected: bool = False
        self.message: Optional[discord.Message] = None
        self.select = VtgResultsDateSelect(valid_matches, caller_id, target_squad=target_squad)
        self.add_item(self.select)

    async def on_timeout(self):
        # Якщо користувач так і не обрав день — видаляємо меню
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
