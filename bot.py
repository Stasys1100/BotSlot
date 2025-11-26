# bot.py
# Discord бот для VTG:
# - Імпорт mission.sqm (текстовий) і побудова відділень зі слотами
# - Правильний формат: "Назва відділення" окремо + нижче всі слоти, включно з "Командир відділення"
# - Фільтр шуму (службові токени, моди, прапори, ідентифікатори)
# - Підтримка фільтрів по індексу (можна кілька): !імпорт_sqm 1-2 2-5
# - Об'єднання дублікатів заголовків (слоти з кількох класів злиті в один блок)
# - Групування за сторонами (ЗСУ / ЗС РФ/ПВК / Союзники / Невідомо) — для кращої читабельності
# - UI для "запис слоти": Embed + кнопки зайняти/відмовитись
# - Модал для зняття слотів (адмін)
# - Команди: стоп, оновити (деплой), статус
# - Нагадування VTG + анонс перезапуску

import os
import re
import html
import difflib
import asyncio
import logging
import time
import subprocess
import datetime
from typing import List, Tuple, Dict, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Логування
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# ─────────────────────────────────────────────────────────────────────────────
# ENV / INIT
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 0)
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 0)

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Сесії UI для "запис слоти"
sessions: Dict[int, dict] = {}
claims: Dict[tuple[int, int], list] = {}
processed_messages: set[int] = set()

# Стоп-флаги
_stop_sending_global = False
_stop_sending_by_channel: Dict[int, bool] = {}

DEFAULT_TITLE = "Відділення"

# Debounce для імпорту
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0  # сек

# ─────────────────────────────────────────────────────────────────────────────
# Виявлення слотів (ключові слова)
# ─────────────────────────────────────────────────────────────────────────────
SLOT_KEYWORDS = [
    r'командир', r'командир відділен', r'командир сторони', r'командир екіпаж',
    r'пілот', r'оператор', r'наводчик', r'санитар', r'медик',
    r'гренадер', r'гранатометник', r'кулеметник', r'стрілець',
    r'старший стрілець', r'снайпер', r'корегувальник', r'механик-вод',
    # англ для союзників / різні моди
    r'squad leader', r'team leader', r'automatic rifleman', r'grenadier',
    r'rifleman', r'designated marksman', r'at gunner', r'machine gunner',
    r'medic', r'drone operator', r'uav operator', r'gunner', r'loader', r'driver', r'comms'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# ─────────────────────────────────────────────────────────────────────────────
# Хелпери очищення/нормалізації
# ─────────────────────────────────────────────────────────────────────────────
def is_noise(s: str) -> bool:
    """
    Відсікає службовий шум: літерали, капс/ідентифікатори, моди/прапори/атрибути, моделі,
    one-off техніка без контексту, службові назви.
    """
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()

    # Часті службові літерали
    noise_literals = {
        "none","null","true","false",
        "army","default","platoon","standard","nochange",
        "uk","ukr","honor",
        "capture_1","defaultred","standardred",
        "everyone",
        "відділення","ввідділення",
        "зс рф та пвк",
    }
    if low in noise_literals:
        return True

    # чисті числа, або суцільний капс/ідентифікатор
    if re.fullmatch(r'\d+', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True

    # моди/прапори/сервісні
    if ("rhs_" in low) or ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low):
        return True
    if low.startswith("door_") or low.startswith("hatch") or "snorkel" in low or "plate" in low or "trunk" in low:
        return True

    # одноразова техніка без контексту (рядок-ідентифікатор)
    oneoff_tech = {
        "mavicblue1","mavicblue2","mavicred1","mavicred2",
        "m113","m113a3","bmp","bmp-2","бмп-2",
        "mt-лб","gaz-66","gaз-66","tigr","тигр","bradley"
    }
    if low in oneoff_tech:
        return True

    # моделі/персонажі
    if s.startswith("Male") and ("ENG" in s or "PER" in s or "RUS" in s):
        return True

    return False

def strip_quotes_semicolons(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'^[\'"]+|[\'"]+$', '', s.strip())
    return re.sub(r';+$', '', s).strip()

def extract_structured_text(raw: str) -> str:
    """
    Витягує текст з description/value/<t> блоків, чистить HTML/escape.
    """
    if not raw:
        return ""
    s = html.unescape(raw)
    attrs = [m.group(1) for m in re.finditer(r'(?:value|description)\s*=\s*"([^"]+)"', s, flags=re.IGNORECASE)]
    t_chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if attrs or t_chunks:
        combined = " ".join(attrs + t_chunks)
        combined = re.sub(r'<[^>]+>', ' ', combined)
        combined = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', combined)
        return re.sub(r'\s{2,}', ' ', combined).strip(' "\'')
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    return re.sub(r'\s{2,}', ' ', s).strip(' "\'')

def looks_like_code_block(s: str) -> bool:
    if not s:
        return True
    if re.search(r'\b(condition|expression|init|compile|preprocessfilelinenumbers|thislist|playerSide|vehicle player)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'\\n|\\r|\\t', s):
        return True
    if re.search(r'[{}()\[\];=<>!|&\\]', s) and len(re.findall(r'[A-Za-zА-Яа-ЯЁёЇїІіЄєҐґ]', s)) < 5:
        return True
    return False

def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'^\s*\d+\.\s*', '', s)
    s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
    return s.strip(' "\'')

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    return s.strip(" \t\n\r-–—")

def dedupe_preserve_order(items: List[str], fuzzy_threshold: float = 0.8) -> List[str]:
    """
    Унікалізація зі збереженням порядку. Злиття схожих назв.
    """
    out: List[str] = []
    for s in items:
        s_norm = s.strip()
        if not s_norm:
            continue
        merged = False
        for i, e in enumerate(out):
            if difflib.SequenceMatcher(None, s_norm.lower(), e.lower()).ratio() >= fuzzy_threshold:
                merged = True
                break
        if not merged:
            out.append(s_norm)
    return out

def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("cp1251", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# Визначення заголовків
# ─────────────────────────────────────────────────────────────────────────────
TITLE_PATTERN = re.compile(
    r'@Альфа|@Alpha|Штаб|бригада|Окрема|відділення|Піхотне|ОМБр|ССО|ГУР|артилерій|ЧВК|армейский|мотострелков|Infantry Squad|Squad|Regiment|Battalion|SEAL Team',
    flags=re.IGNORECASE
)

def split_commander_from_title(s: str) -> Tuple[str, Optional[str]]:
    """
    Розділяє "Командир відділення | ENG @Альфа 2-2 | 93-тя ОМБр …" на:
    - title: "@Альфа 2-2 | 93-тя ОМБр …"
    - commander_slot: "Командир відділення" (або локалізоване)
    Якщо не знаходить сигнатуру командира — повертає (s, None).
    """
    s_clean = s.strip()
    # Українська/російська
    m = re.match(r'^\s*(Командир [^|@]*)(?:\s*\|\s*ENG)?\s*@', s_clean, flags=re.IGNORECASE)
    if m:
        commander = m.group(1)
        title = re.sub(r'^\s*' + re.escape(m.group(0)).replace('@', '@'), '@', s_clean).strip()
        # Тепер title починається з @Альфа... решту беремо як є
        return title, commander
    # Англійська (Squad Leader ...)
    m2 = re.match(r'^\s*(Squad Leader[^|@]*)(?:\s*\|\s*[A-Z]+)?\s*@', s_clean, flags=re.IGNORECASE)
    if m2:
        commander = m2.group(1)
        title = re.sub(r'^\s*' + re.escape(m2.group(0)).replace('@', '@'), '@', s_clean).strip()
        return title, commander
    # Якщо не знайдено шаблону — інколи заголовок вже без "Командир ...", тоді повертаємо як є
    return s_clean, None

# ─────────────────────────────────────────────────────────────────────────────
# Парсер місії
# ─────────────────────────────────────────────────────────────────────────────
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Витягує (title, [slots]) з description/value/<t>... ; фільтрує шум.
    - Заголовок: наявність '|' або TITLE_PATTERN. Розділяємо командира з заголовка.
    - Слоти: нумерація або ключові слова + всі інші рядки між заголовками (якщо не шум і не код).
    - Дублікат заголовку → об'єднання слотів, зберігаючи порядок і унікальність.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # зібрати candidate тексти
    candidates = [html.unescape(m.group(1)).strip()
                  for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    candidates += [html.unescape(m).strip()
                   for m in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)]

    if not candidates:
        return []

    groups: Dict[str, List[str]] = {}  # title_norm -> slots
    cur_title: Optional[str] = None
    cur_slots: List[str] = []

    def flush():
        nonlocal cur_title, cur_slots
        if cur_title is None and cur_slots:
            title_line = DEFAULT_TITLE
        else:
            title_line = cur_title or DEFAULT_TITLE
        if cur_slots:
            # очищення/нормалізація
            slots = []
            for s in cur_slots:
                s2 = clean_line_for_slot(s)
                if s2 and not looks_like_code_block(s2) and not is_noise(s2):
                    slots.append(normalize_slot_name(s2))
            slots = dedupe_preserve_order(slots)
            if slots:
                t_norm = re.sub(r'\s{2,}', ' ', title_line).strip()
                prev = groups.get(t_norm, [])
                # merge
                merged = prev + [x for x in slots if x not in prev]
                groups[t_norm] = merged
        cur_title, cur_slots = None, []

    for raw in candidates:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # Заголовок
        if '|' in s or TITLE_PATTERN.search(s):
            # Розділити "Командир ..." із заголовка
            title_line, commander_slot = split_commander_from_title(s)
            flush()  # закрити попередню групу
            cur_title = title_line
            # Додати командира як перший слот (якщо знайшли)
            if commander_slot:
                cur_slots.append(commander_slot)
            continue

        # Слот за нумерацією або ключовими словами
        if re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s):
            cur_slots.append(s)
            continue

        # Інші рядки між заголовками: додати, якщо не шум і не код
        if not looks_like_code_block(s) and not is_noise(s):
            cur_slots.append(s)

    flush()

    # перетворити в список
    return [(title, slots) for title, slots in groups.items()]

# ─────────────────────────────────────────────────────────────────────────────
# Детектор сторони (для групування виводу)
# ─────────────────────────────────────────────────────────────────────────────
def detect_side_from_title(title: str) -> str:
    t = title.lower()
    # Українська сторона
    if any(k in t for k in ["омбр", "зсу", "гуp", "ссо", "окрема", "бригада", "@альфа", "альфа", "холодний яр"]):
        return "ЗСУ"
    # Російська / ПВК
    if any(k in t for k in ["армейский", "чвк", "мотострелковая", "корпус", "отдельная", "армия", "72-я", "3-й ак"]):
        return "ЗС РФ/ПВК"
    # Союзники/нато/англомовне
    if any(k in t for k in ["regiment", "battalion", "seal team", "mechanized squad", "@alpha"]):
        return "Союзники"
    return "Невідомо"

# ─────────────────────────────────────────────────────────────────────────────
# UI helpers для "запис слоти"
# ─────────────────────────────────────────────────────────────────────────────
def build_embed(sess: dict) -> discord.Embed:
    embed = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    owners = sess.get("owners", [None] * len(sess["lines"]))
    for i, (text, owner) in enumerate(zip(sess["lines"], owners)):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    embed.description = "\n".join(lines)
    return embed

class SlotButton(Button):
    def __init__(self, sid: int, idx: int):
        owner = sessions[sid]["owners"][idx]
        free = owner is None
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        # Заборона множинних слотів у тій же гілці
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == sess["channel_id"] and user in s.get("owners", []):
                    return await inter.response.send_message("⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True)
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        return await inter.response.send_message(f"⚠️ Цей слот зайнято {owner.mention}.", ephemeral=True)

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# Модал для зняття слотів
class RemoveSlotModal(Modal):
    def __init__(self, sid: int, idx: int):
        super().__init__(title="Причина звільнення")
        self.sid, self.idx = sid, idx
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        reason = self.reason.value
        if not owner:
            return await inter.response.send_message(f"⚠️ Слот #{self.idx+1} вже вільний.", ephemeral=True)
        sess["owners"][self.idx] = None
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except Exception:
                pass
        try:
            await owner.send(f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
        except Exception:
            pass
        await inter.response.send_message(f"✅ Слот #{self.idx+1} звільнено.", ephemeral=True)

class RemoveSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(label=str(idx+1), style=discord.ButtonStyle.danger, custom_id=f"remove-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(RemoveSlotModal(self.sid, self.idx))

class RemoveSlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(RemoveSlotButton(sid, idx))

# ─────────────────────────────────────────────────────────────────────────────
# Команди
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context, *filter_ids: str):
    """
    Імпорт текстового mission.sqm.
    - Без аргументів: виводить усі відділення по групах (дублікати заголовків об'єднані).
    - З аргументами (наприклад "2-2" або "1-2 2-5"): показує лише відповідні заголовки (як окремий токен), для всіх сторін.
    Формат виводу: Назва відділення (без "Командир ...") окремим рядком, далі повний список слотів з "Командир відділення" як перший.
    """
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть mission.sqm або mission.txt")

    att = ctx.message.attachments[0]
    key = f"{ctx.message.id}:{att.id}"
    now = time.time()
    # cleanup старих записів
    for k, t in list(_recent_imports.items()):
        if now - t > _RECENT_IMPORTS_TTL:
            _recent_imports.pop(k, None)
    if key in _recent_imports:
        return await ctx.send("⚠️ Ця команда вже обробляється (повторне надходження).")
    _recent_imports[key] = now

    # читання
    try:
        raw = await att.read()
        text = decode_bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as e:
        _recent_imports.pop(key, None)
        logger.exception("Failed to read attachment")
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    # парсинг
    try:
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # фільтрація по індексах (як окремий токен), якщо передані аргументи
    if filter_ids:
        patterns = [re.compile(rf'\b{re.escape(fid)}\b') for fid in filter_ids]
        filtered = []
        seen_titles = set()
        for title, slots in groups:
            if any(p.search(title or "") for p in patterns):
                t_norm = re.sub(r'\s{2,}', ' ', title).strip()
                if t_norm in seen_titles:
                    # об'єднання слотів — на випадок повторних класів
                    idx = next((i for i, (tt, _) in enumerate(filtered) if tt == t_norm), None)
                    if idx is not None:
                        prev_slots = filtered[idx][1]
                        merged = prev_slots + [x for x in slots if x not in prev_slots]
                        filtered[idx] = (t_norm, merged)
                    continue
                seen_titles.add(t_norm)
                filtered.append((t_norm, slots))
        groups = filtered

    if not groups:
        _recent_imports.pop(key, None)
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # групування за сторонами
    by_side: Dict[str, List[Tuple[str, List[str]]]] = {"ЗСУ": [], "ЗС РФ/ПВК": [], "Союзники": [], "Невідомо": []}
    for title, slots in groups:
        side = detect_side_from_title(title)
        by_side[side].append((title, slots))

    # відправка з правильним форматом
    sent = 0
    for side in ("ЗСУ", "ЗС РФ/ПВК", "Союзники", "Невідомо"):
        blocks = by_side[side]
        if not blocks:
            continue
        # секційний заголовок
        await ctx.send(f"```{side}```")
        for title, slots in blocks:
            out = title + "\n" + "\n".join(slots)
            # якщо дуже довго — чанк
            parts = out.splitlines()
            chunk, count = [], 0
            for line in parts:
                chunk.append(line)
                count += 1
                if count >= 40:
                    await ctx.send(f"```{chr(10).join(chunk)}```")
                    chunk, count = [], 0
                    await asyncio.sleep(0)
            if chunk:
                await ctx.send(f"```{chr(10).join(chunk)}```")
                sent += 1
            await asyncio.sleep(0.08)

    _recent_imports.pop(key, None)
    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

@bot.command(name="стоп", aliases=["stop"])
async def стоп(ctx: commands.Context):
    global _stop_sending_global, _stop_sending_by_channel
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    _stop_sending_global = True
    _stop_sending_by_channel[ctx.channel.id] = True
    await ctx.send("⏹️ Зупиняю відправку відділень...")

@bot.command(name="оновити", aliases=["update"])
async def _оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановлено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Деплой тригерено!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

# ─────────────────────────────────────────────────────────────────────────────
# Нагадування VTG
# ─────────────────────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    # приклад: п'ятниця/неділя, 19:30 — ping
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\n**Сбор VTG**")
                except Exception:
                    logger.exception("vtg_reminder send failed")

# ─────────────────────────────────────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info("Bot ready: %s", bot.user)
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    embed = discord.Embed(title="🔄 Бот перезапущено", description=f"📦 Commit: `{commit}`", color=discord.Color.green())
    for guild in bot.guilds:
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages, guild.text_channels)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                logger.exception("Failed to announce restart")
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return
    # Автоматичний тригер створення сесії "запис слоти" зі списку в повідомленні
    if "запис слоти" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = next((u for u in message.mentions if f"<@{u.id}>" in txt or f"<@!{u.id}>" in txt), None)
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = txt
        slots = slots[:25]
        owners = owners[:len(slots)]
        sess = {"title": header or DEFAULT_TITLE, "lines": slots, "owners": owners, "channel_id": message.channel.id}
        embed = build_embed(sess)
        sent = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))
    await bot.process_commands(message)

# ─────────────────────────────────────────────────────────────────────────────
# Команда зняття слотів через UI (адмін)
# ─────────────────────────────────────────────────────────────────────────────
@bot.command(name="зняти")
async def зняти(ctx: commands.Context, session_msg_id: int):
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:", view=RemoveSlotView(session_msg_id))

# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
