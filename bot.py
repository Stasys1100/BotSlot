# bot.py
# Discord бот: імпорт mission.sqm, фільтрація, вибір відділень по індексу, UI для слотів, статус/деплой/нагадування.

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

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# ─── ENV / INIT ─────────────────────────────────────────────────────────────
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

sessions: Dict[int, dict] = {}
claims: Dict[tuple[int, int], list] = {}
processed_messages: set[int] = set()

_stop_sending_global = False
_stop_sending_by_channel: Dict[int, bool] = {}

DEFAULT_TITLE = "Відділення"

# Debounce для імпорту
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0  # сек

# ─── Slot detection ─────────────────────────────────────────────────────────
SLOT_KEYWORDS = [
    r'командир', r'командир відділен', r'командир сторони', r'командир екіпаж',
    r'пілот', r'оператор', r'наводчик', r'санитар', r'медик',
    r'гренадер', r'гранатометник', r'кулеметник', r'стрілець',
    r'старший стрілець', r'снайпер', r'корегувальник', r'механик-вод'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# ─── Helpers ────────────────────────────────────────────────────────────────
def is_noise(s: str) -> bool:
    """Відсікає технічний шум, моди, прапори, моделі, службові токени."""
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()

    noise_literals = {
        "none","null","true","false",
        "army","default","platoon","standard","nochange",
        "uk","ukr","honor","capture_1","defaultred","standardred",
        "відділення","ввідділення","everyone"
    }
    if low in noise_literals:
        return True

    if re.fullmatch(r'\d+', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True

    if ("rhs_" in low) or ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low):
        return True
    if low.startswith("door_") or low.startswith("hatch") or "snorkel" in low or "plate" in low or "trunk" in low:
        return True

    if s.startswith("Male") and ("ENG" in s or "PER" in s or "RUS" in s):
        return True

    return False

def strip_quotes_semicolons(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'^[\'"]+|[\'"]+$', '', s.strip())
    return re.sub(r';+$', '', s).strip()

def extract_structured_text(raw: str) -> str:
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

def dedupe_preserve_order(items: List[str], fuzzy_threshold: float = 0.78) -> List[str]:
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

# ─── Parser ─────────────────────────────────────────────────────────────────
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Витягує (title, [slots]) з description/value і <t>…</t>.
    Фільтрує шум. Заголовки: містять '|' чи ключові слова (Альфа/Штаб/Бригада/ОМБр/ССО/ГУР/ЧВК/армейский/відділення/Піхотне).
    Слоти: нумерація або ключові слова; також всі інші рядки між заголовками, якщо не шум і не код.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    matches = [(m.start(), html.unescape(m.group(1)).strip())
               for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    if not matches:
        t_matches = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)
        matches = [(0, html.unescape(m).strip()) for m in t_matches]
    if not matches:
        return []

    groups: List[Tuple[str, List[str]]] = []
    cur_title: Optional[str] = None
    cur_slots: List[str] = []

    def flush():
        nonlocal cur_title, cur_slots
        if cur_title or cur_slots:
            slots = [normalize_slot_name(s) for s in cur_slots if s and not looks_like_code_block(s) and not is_noise(s)]
            slots = dedupe_preserve_order(slots)
            if slots:
                title_line = re.sub(r'\s{2,}', ' ', (cur_title or DEFAULT_TITLE)).strip()
                groups.append((title_line, slots))
        cur_title, cur_slots = None, []

    for _, raw in matches:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # Заголовок
        if '|' in s or re.search(r'@Альфа|Штаб|бригада|Окрема|відділення|Піхотне|ОМБр|ССО|ГУР|артилерій|ЧВК|армейский', s, flags=re.IGNORECASE):
            flush()
            cur_title = s
            continue

        # Слот за нумерацією або ключовими словами
        if re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if slot and not looks_like_code_block(slot) and not is_noise(slot):
                cur_slots.append(slot)
            continue

        # Інші рядки між заголовками — додаємо як слоти, якщо не шум і не код
        if not looks_like_code_block(s) and not is_noise(s):
            cur_slots.append(clean_line_for_slot(s))

    flush()

    # прибрати дублікати заголовків
    seen_titles = set()
    unique_groups: List[Tuple[str, List[str]]] = []
    for title, slots in groups:
        t_norm = re.sub(r'\s{2,}', ' ', title).strip()
        if t_norm in seen_titles:
            continue
        seen_titles.add(t_norm)
        unique_groups.append((t_norm, slots))
    return unique_groups

# ─── UI helpers ─────────────────────────────────────────────────────────────
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

# ─── Commands ───────────────────────────────────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context, filter_id: str = None):
    """
    Імпорт текстового mission.sqm. Якщо задано filter_id (наприклад "2-2"),
    повертає всі групи, де заголовок містить індекс як окремий токен (для всіх сторін).
    """
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть mission.sqm або mission.txt")

    att = ctx.message.attachments[0]
    key = f"{ctx.message.id}:{att.id}"
    now = time.time()
    for k, t in list(_recent_imports.items()):
        if now - t > _RECENT_IMPORTS_TTL:
            _recent_imports.pop(k, None)
    if key in _recent_imports:
        return await ctx.send("⚠️ Ця команда вже обробляється (повторне надходження).")
    _recent_imports[key] = now

    try:
        raw = await att.read()
        text = decode_bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as e:
        _recent_imports.pop(key, None)
        logger.exception("Failed to read attachment")
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    try:
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # Якщо задано фільтр — відбираємо всі збіги по індексу (як окремий токен) і прибираємо дублікати
    if filter_id:
        pattern = re.compile(rf'\b{re.escape(filter_id)}\b')
        groups = [(t, s) for (t, s) in groups if pattern.search(t or "")]
        # Прибрати дублікати ще раз (на випадок дрібних різниць)
        seen = set()
        filtered_unique = []
        for title, slots in groups:
            t_norm = re.sub(r'\s{2,}', ' ', title).strip()
            if t_norm in seen:
                continue
            seen.add(t_norm)
            filtered_unique.append((t_norm, slots))
        groups = filtered_unique

        if not groups:
            _recent_imports.pop(key, None)
            return await ctx.send(f"⚠️ Не знайдено відділень з індексом {filter_id}.")

    # Якщо фільтра немає і нічого не знайдено — fallback: нумеровані рядки
    if not filter_id and not groups:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2) and not is_noise(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order(cleaned)
        if cleaned:
            groups = [(DEFAULT_TITLE, cleaned)]

    if not groups:
        _recent_imports.pop(key, None)
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    sent = 0
    for title, slots in groups:
        out = "\n".join([title] + slots)
        await ctx.send(f"```{out}```")
        sent += 1
        await asyncio.sleep(0.1)

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

# ─── Reminder (optional) ─────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\n**Сбор VTG**")
                except Exception:
                    logger.exception("vtg_reminder send failed")

# ─── on_ready / on_message ──────────────────────────────────────────────────
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

# ─── Run ────────────────────────────────────────────────────────────────────
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
