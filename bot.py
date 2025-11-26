# bot.py
# Discord бот: приймає дебінаризований mission.sqm і повертає тільки відділення + слоти
# .env: DISCORD_TOKEN (обов'язково), ADMIN_CHANNEL_ID (опц.), VTG_CHANNEL_ID (опц.), DEPLOY_HOOK_URL (опц.)

import os
import re
import io
import subprocess
import datetime
import html
import difflib
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict, Any

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# optional keep_alive (if you have a keep_alive.py)
try:
    from keep_alive import keep_alive
    keep_alive()
except Exception:
    pass

# ─── ENV / INIT ───────────────────────────────────────────────────────────────
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

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}
claims: dict[tuple[int,int], list] = {}
request_counter = 0

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Відділення"

# ─── Reminder (optional) ──────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\n**Сбор VTG**")
                except:
                    pass

# ─── Embed builder for sessions (preserved) ───────────────────────────────────
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

# ─── Helpers: cleaning / normalization ────────────────────────────────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    # try to extract <t> inner content first
    chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if chunks:
        inner = " ".join(chunks)
        inner = re.sub(r'<[^>]+>', ' ', inner)
        inner = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', inner)
        inner = re.sub(r'\s{2,}', ' ', inner).strip(' "\'').strip()
        return inner
    # fallback: strip tags
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    return s

def clean_slot_value(raw: str) -> str:
    if raw is None:
        return ""
    s = extract_structured_text(raw)
    s = s.replace('\\', '/')
    s = re.sub(r'\.sqf\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\.pbo\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bhttps?://\S+\b', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    return s

def split_combined_slot(s: str) -> List[str]:
    if not s:
        return []
    s = s.strip()
    s = re.sub(r'[\r\t]+', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s)
    parts = []
    for chunk in re.split(r'\n+|\r+|\s{2,}', s):
        chunk = chunk.strip()
        if not chunk:
            continue
        subparts = re.split(r'(?<=[\.\!\?])\s+(?=[А-ЯІЇЄҐA-Z])', chunk)
        for sp in subparts:
            sp = sp.strip()
            if not sp:
                continue
            if re.search(r'[\.\!\?].*[\.\!\?]', sp):
                more = re.split(r'(?<=[\.\!\?])\s*', sp)
                for m in more:
                    m = m.strip()
                    if m:
                        parts.append(m)
            else:
                parts.append(sp)
    cleaned = []
    for p in parts:
        p = p.strip()
        p = re.sub(r'([!?.]){2,}', r'\1', p)
        p = re.sub(r'\s+([!?.,:;])', r'\1', p)
        p = p.strip(" \t\n\r")
        if p:
            cleaned.append(p)
    return cleaned

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    s = re.sub(r'([!?.]){2,}', r'\1', s)
    s = s.strip(" \t\n\r-–—")
    return s

def is_template_slot(s: str) -> bool:
    if not s:
        return True
    low = s.lower().strip()
    if low in ("слот", "-", "—", "n/a", "none", "пусто"):
        return True
    if re.fullmatch(r'^[\W_]{1,5}$', low):
        return True
    return False

def dedupe_preserve_order(items: List[str], fuzzy_threshold: float = 0.78) -> List[str]:
    def cyr_score(x: str) -> int:
        return len(re.findall(r'[\u0400-\u04FF]', x))
    out: List[str] = []
    for s in items:
        s_norm = s.strip()
        if not s_norm:
            continue
        merged = False
        for i, existing in enumerate(out):
            ratio = difflib.SequenceMatcher(None, existing.lower(), s_norm.lower()).ratio()
            if ratio >= fuzzy_threshold:
                if cyr_score(s_norm) > cyr_score(existing):
                    out[i] = s_norm
                elif cyr_score(s_norm) == cyr_score(existing):
                    if len(s_norm) > len(existing):
                        out[i] = s_norm
                merged = True
                break
        if not merged:
            out.append(s_norm)
    return out

# ─── Heuristics: filter technical lines and extract only units + slots ───────
def looks_like_tech_line(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    if re.search(r'\b(className|addons?|url|preview|version|randomSeed|ScenarioDatas|Datasource|author|classNamer)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'https?://', s, flags=re.IGNORECASE):
        return True
    # рядки, що виглядають як модулі/клас-ідентифікатори (без пробілів або з підкресленнями)
    if ('_' in s and len(s.split()) == 1) or (len(s.split()) == 1 and re.search(r'[A-Za-z0-9_]', s)):
        return True
    # дуже короткі або лише цифри
    if re.fullmatch(r'[\d\W]{1,4}', s):
        return True
    return False

def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Повертає список (title, [slot1, slot2, ...]) у форматі, готовому для виводу.
    Агресивно фільтрує технічні рядки і addon-списки.
    """
    # розбити на рядки і попередньо очистити
    raw_lines = [ln.strip() for ln in text.replace('\r\n', '\n').splitlines()]
    # видалити явні control/порожні рядки
    raw_lines = [ln for ln in raw_lines if ln and not re.fullmatch(r'[\x00-\x1f\x7f-\x9f]+', ln)]

    # Якщо файл починається з великого списку addon/class (багато рядків без пробілів або з підкресленнями),
    # пропустимо перший блок таких рядків — це джерело "мусору".
    cleaned_lines: List[str] = []
    i = 0
    n = len(raw_lines)
    # пропускаємо початковий блок технічних рядків довжиною >= 5
    tech_block_len = 0
    while i < n and looks_like_tech_line(raw_lines[i]):
        tech_block_len += 1
        i += 1
    if tech_block_len >= 5:
        # пропустили великий технічний блок
        pass
    else:
        # якщо блок короткий — не пропускаємо
        i = 0

    # з i починаємо збирати релевантні рядки
    while i < n:
        ln = raw_lines[i]
        # якщо рядок явно технічний — пропускаємо
        if looks_like_tech_line(ln):
            i += 1
            continue
        cleaned_lines.append(ln)
        i += 1

    # Тепер з cleaned_lines шукаємо заголовки і їхні нумеровані слоти
    groups: List[Tuple[str, List[str]]] = []
    i = 0
    m = len(cleaned_lines)
    while i < m:
        ln = cleaned_lines[i]
        # Якщо рядок виглядає як заголовок (містить '|' або '@' або слово "відділення" або довгий опис) — беремо як title
        if '|' in ln or '@' in ln or re.search(r'\b(відділен|відділ|бригада|екіпаж|crew|командир)\b', ln, flags=re.IGNORECASE):
            title = ln
            # збираємо наступні нумеровані слоти
            slots: List[str] = []
            j = i + 1
            while j < m and re.match(r'^\s*\d+\.\s*', cleaned_lines[j]):
                slot = re.sub(r'^\s*\d+\.\s*', '', cleaned_lines[j]).strip()
                if slot and not looks_like_tech_line(slot):
                    slots.append(slot)
                j += 1
            # якщо після заголовка немає нумерованих слотів, можливо слоти йдуть в одному рядку або через коми — спробуємо знайти наступні 10 рядків з нумерацією в середині
            if not slots:
                k = i + 1
                while k < m and len(slots) < 25:
                    if re.search(r'\d+\.', cleaned_lines[k]):
                        found = re.findall(r'\d+\.\s*([^0-9]+?)(?=(?:\d+\.|$))', cleaned_lines[k])
                        for f in found:
                            f2 = f.strip()
                            if f2 and not looks_like_tech_line(f2):
                                slots.append(f2)
                    k += 1
            if slots:
                groups.append((title.strip(), [normalize_slot_name(s) for s in slots]))
                i = j
                continue
            else:
                i += 1
                continue

        # Якщо рядок починається з нумерації — зібрати послідовність слотів і створити заголовок "Відділення"
        if re.match(r'^\s*\d+\.\s*', ln):
            slots = []
            while i < m and re.match(r'^\s*\d+\.\s*', cleaned_lines[i]):
                slot = re.sub(r'^\s*\d+\.\s*', '', cleaned_lines[i]).strip()
                if slot and not looks_like_tech_line(slot):
                    slots.append(normalize_slot_name(slot))
                i += 1
            if slots:
                groups.append(("Відділення", slots))
            continue

        # Інакше — можливо це одиночний рядок, який містить заголовок і слоти в одному рядку (через крапки або коми)
        if '|' in ln and re.search(r'\d+\.', ln):
            parts = ln.split('|', 1)
            title = parts[0].strip()
            rest = parts[1]
            found = re.findall(r'\d+\.\s*([^0-9]+?)(?=(?:\d+\.|$))', rest)
            slots = [normalize_slot_name(f.strip()) for f in found if f.strip() and not looks_like_tech_line(f.strip())]
            if slots:
                groups.append((title, slots))
                i += 1
                continue

        i += 1

    # фінальна очистка: видалити дублікати та порожні
    final: List[Tuple[str, List[str]]] = []
    for title, slots in groups:
        seen = set()
        cleaned_slots = []
        for s in slots:
            key = s.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned_slots.append(s)
        if cleaned_slots:
            final.append((re.sub(r'\s{2,}', ' ', title).strip(), cleaned_slots))
    return final

# ─── Оновлена команда: імпорт_sqm_decoded (без попереднього "не знайдено") ─────
@bot.command(name="імпорт_sqm_decoded", aliases=["import_sqm", "import_sqm_decoded", "імпорт_sqm"])
async def імпорт_sqm_decoded(ctx: commands.Context):
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть текстовий mission.sqm до повідомлення.")
    attachment = ctx.message.attachments[0]

    try:
        raw = await attachment.read()
        if isinstance(raw, (bytes, bytearray)):
            try:
                sqm_text = raw.decode("utf-8")
            except Exception:
                sqm_text = raw.decode("cp1251", errors="replace")
        else:
            sqm_text = str(raw)
    except Exception as e:
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    groups = extract_units_and_slots(sqm_text)

    if not groups:
        # Якщо нічого не знайшлося — спробуємо ще агресивно витягнути всі рядки з нумерацією і повернути їх як одне відділення
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', sqm_text, flags=re.MULTILINE)
        cleaned = [normalize_slot_name(clean_slot_value(s)) for s in all_numbered if s and not looks_like_tech_line(s)]
        cleaned = [s for s in cleaned if s]
        cleaned = dedupe_preserve_order(cleaned)
        if cleaned:
            groups = [("Відділення", cleaned)]

    if not groups:
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # Формат виводу: заголовок у першому рядку, потім слоти (без нумерації), як просив
    for title, slots in groups:
        title_line = re.sub(r'\s{2,}', ' ', title).strip()
        lines = [title_line] + slots
        out_text = "\n".join(lines)
        try:
            await ctx.send(f"```{out_text}```")
        except Exception:
            parts = out_text.splitlines()
            chunk = []
            for i, line in enumerate(parts):
                chunk.append(line)
                if (i + 1) % 40 == 0:
                    await ctx.send(f"```{chr(10).join(chunk)}```")
                    chunk = []
            if chunk:
                await ctx.send(f"```{chr(10).join(chunk)}```")

    await ctx.send(f"✅ Готово. Опубліковано відділень: {len(groups)}.")

# ─── on_ready / on_message / session UI (existing logic preserved) ───────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
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
            except:
                pass
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
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))
    await bot.process_commands(message)

# ─── service commands ────────────────────────────────────────────────────────
@bot.command(name="оновити", aliases=["update"])
async def _оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Деплой тригерено!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

@bot.command(name="gitpush")
async def _gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                         inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m \"Оновлення слота\"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── Run ─────────────────────────────────────────────────────────────────────
if not TOKEN:
    print("DISCORD_TOKEN not set in environment")
else:
    bot.run(TOKEN)
