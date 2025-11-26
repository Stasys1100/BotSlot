# bot.py
# Discord бот: приймає текстовий mission.sqm (або .txt) і повертає відділення + слоти
# .env: DISCORD_TOKEN (обов'язково), ADMIN_CHANNEL_ID (опц.), VTG_CHANNEL_ID (опц.), DEPLOY_HOOK_URL (опц.)

import os
import re
import html
import difflib
import asyncio
import logging
import time
import subprocess
from typing import List, Tuple, Dict
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from dotenv import load_dotenv

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# ─── ENV / INIT ─────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 0)
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 0)

sessions: Dict[int, dict] = {}
claims: Dict[tuple[int, int], list] = {}
request_counter = 0
processed_messages: set[int] = set()

# стоп-флаги
_stop_sending_global = False
_stop_sending_by_channel: Dict[int, bool] = {}

DEFAULT_TITLE = "Відділення"

# Debounce для імпорту (захист від дублювання)
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0  # секунд

# ─── Slot detection ─────────────────────────────────────────────────────────
SLOT_KEYWORDS = [
    r'командир', r'командир відділен', r'командир сторони', r'пілот', r'оператор',
    r'оператор бпла', r'медик', r'санитар', r'гренадер', r'гранатометник',
    r'кулеметник', r'стрілець', r'механик', r'механік'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# ─── Helpers ────────────────────────────────────────────────────────────────
def strip_quotes_semicolons(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r'^[\'"]+|[\'"]+$', '', s)
    s = re.sub(r';+$', '', s)
    return s.strip()

def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    # зібрати value/description атрибути, якщо вони є
    attrs = [m.group(1) for m in re.finditer(r'(?:value|description)\s*=\s*"([^"]+)"', s, flags=re.IGNORECASE)]
    if attrs:
        combined = " ".join(attrs)
        combined = re.sub(r'<[^>]+>', ' ', combined)
        combined = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', combined)
        return re.sub(r'\s{2,}', ' ', combined).strip(' "\'')
    # fallback: прибрати тегові конструкції
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
    if re.search(r'[{}()\[\];=<>!|&\\]', s) and len(re.findall(r'[A-Za-zА-Яа-яЁёЇїІіЄєҐґ]', s)) < 5:
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
        for i, existing in enumerate(out):
            ratio = difflib.SequenceMatcher(None, existing.lower(), s_norm.lower()).ratio()
            if ratio >= fuzzy_threshold:
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
    Витягує всі description/value і групує їх у (title, [slots...]).
    Логіка:
    - Рядки з '|' або @Альфа/Штаб/бригада/Окрема/відділення -> заголовок.
    - Рядки з нумерацією або ключовими словами -> слот.
    - Короткі рядки з ENG/MED/технікою -> слот.
    - Інакше: або заголовок-кандидат, або слот (якщо вже є title).
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    matches = [(m.start(), html.unescape(m.group(1)).strip())
               for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    if not matches:
        # інколи дані можуть бути в <t>...</t> блоках
        t_matches = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)
        matches = [(0, html.unescape(m).strip()) for m in t_matches]
    if not matches:
        return []

    groups: List[Tuple[str, List[str]]] = []
    cur_title: str | None = None
    cur_slots: List[str] = []
    rejected: List[str] = []

    def flush():
        nonlocal cur_title, cur_slots
        if cur_title or cur_slots:
            slots = [normalize_slot_name(s) for s in cur_slots if s and not looks_like_code_block(s)]
            slots = dedupe_preserve_order(slots)
            if slots:
                title_line = re.sub(r'\s{2,}', ' ', (cur_title or DEFAULT_TITLE)).strip()
                groups.append((title_line, slots))
        cur_title = None
        cur_slots = []

    for _, raw in matches:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s:
            continue

        # Заголовок
        if '|' in s or re.search(r'@Альфа|Штаб|бригада|Окрема|відділення|Піхотне', s, flags=re.IGNORECASE):
            flush()
            cur_title = s
            continue

        # Слот за нумерацією або ключовими словами
        if re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if slot and not looks_like_code_block(slot):
                cur_slots.append(slot)
            else:
                rejected.append(s)
            continue

        # Короткі або технічні токени — трактувати як слот
        if re.search(r'\b(ENG|MED|M113|ZALA|Mavic|FPV|HIMARS|BMP|T-72|M113A3)\b', s, flags=re.IGNORECASE) or len(s.split()) <= 6:
            cur_slots.append(clean_line_for_slot(s))
            continue

        # Інакше — або додаємо як слот до активного заголовка, або робимо новий заголовок
        if cur_title:
            cur_slots.append(clean_line_for_slot(s))
        else:
            if len(s) > 10:
                flush()
                cur_title = s
            else:
                cur_slots.append(clean_line_for_slot(s))

    flush()

    # Фолбек: якщо не знайшли груп, спробувати всі нумеровані рядки
    if not groups:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order(cleaned)
        if cleaned:
            groups = [(DEFAULT_TITLE, cleaned)]

    # Лог відкинутих рядків
    try:
        if rejected:
            with open("parser_debug.log", "w", encoding="utf-8") as f:
                f.write("=== Rejected lines (first 200) ===\n")
                for r in rejected[:200]:
                    f.write(r.replace("\n", " ") + "\n")
    except Exception:
        logger.exception("Failed to write parser_debug.log")

    return groups

# ─── Reminder (optional) ─────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    # За потреби можна додати логіку нагадування
    return

# ─── Embed builder для сесій ────────────────────────────────────────────────
def build_embed(sess: dict) -> discord.Embed:
    embed = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    embed.description = "\n".join(lines)
    return embed

# ─── Мінімальний UI для зайняття слотів ─────────────────────────────────────
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
        if owner is None:
            # уникнути множинних слотів у тій же гілці
            for s in sessions.values():
                if s["channel_id"] == sess["channel_id"] and user in s["owners"]:
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

# ─── Команди ────────────────────────────────────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm", "імпорт_sqm_decoded", "import_sqm_decoded"])
async def імпорт_sqm(ctx: commands.Context):
    # Обмеження на канал (опціонально)
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")

    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть mission.sqm або mission.txt")

    attachment = ctx.message.attachments[0]
    # Debounce: захист від дублювання
    key = f"{ctx.message.id}:{attachment.id}"
    now = time.time()
    # очистити старі ключі
    for k, t in list(_recent_imports.items()):
        if now - t > _RECENT_IMPORTS_TTL:
            _recent_imports.pop(k, None)
    if key in _recent_imports:
        return await ctx.send("⚠️ Ця команда вже обробляється (повторне надходження).")
    _recent_imports[key] = now

    try:
        raw = await attachment.read()
        if isinstance(raw, (bytes, bytearray)):
            text = decode_bytes(raw)
        else:
            text = str(raw)
    except Exception as e:
        _recent_imports.pop(key, None)
        logger.exception("Failed to read attachment")
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    try:
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # Фолбек: якщо парсер нічого не знайшов — зібрати всі нумеровані рядки
    if not groups:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order(cleaned)
        if cleaned:
            groups = [(DEFAULT_TITLE, cleaned)]

    if not groups:
        _recent_imports.pop(key, None)
        logger.info("No groups found in attachment %s (message %s)", attachment.filename, ctx.message.id)
        # дебаг-лог
        try:
            with open("parser_debug.log", "w", encoding="utf-8") as f:
                f.write("No groups found. First 200 description/value occurrences:\n")
                for i, m in enumerate(re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)):
                    if i >= 200:
                        break
                    f.write(m.group(1).replace("\n", " ") + "\n")
        except Exception:
            logger.exception("Failed to write parser_debug.log")
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # Відправка результатів
    sent = 0
    for title, slots in groups:
        if _stop_sending_global or _stop_sending_by_channel.get(ctx.channel.id):
            await ctx.send("⏹️ Відправка зупинена.")
            break
        out_text = "\n".join([title] + slots)
        try:
            await ctx.send(f"```{out_text}```")
            sent += 1
        except Exception:
            # chunk long outputs
            parts = out_text.splitlines()
            chunk = []
            for i, line in enumerate(parts):
                chunk.append(line)
                if (i + 1) % 40 == 0:
                    await ctx.send(f"```{chr(10).join(chunk)}```")
                    chunk = []
                    await asyncio.sleep(0)
            if chunk:
                await ctx.send(f"```{chr(10).join(chunk)}```")
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
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

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
        slots, owners = slots[:25], owners[:len(slots)]
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
