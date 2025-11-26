# bot.py
# Discord бот: приймає текстовий mission.sqm (або .txt) і повертає відділення + слоти
# .env: DISCORD_TOKEN (обов'язково), ADMIN_CHANNEL_ID (опц.), VTG_CHANNEL_ID (опц.), DEPLOY_HOOK_URL (опц.)

import os
import re
import subprocess
import datetime
import html
import difflib
import asyncio
import logging
import time
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# optional keep_alive (if present)
try:
    from keep_alive import keep_alive
    keep_alive()
except Exception:
    pass

# ─── Налаштування логування ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

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
sessions: Dict[int, dict] = {}
claims: Dict[tuple[int,int], list] = {}
request_counter = 0

# стоп-флаги
_stop_sending_global = False
_stop_sending_by_channel: Dict[int, bool] = {}

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Відділення"

# Дебаунс: щоб уникнути повторної обробки одного й того ж повідомлення/вкладення
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0  # секунди

# Ключові слова слотів
SLOT_KEYWORDS = [
    r'командир', r'командир відділен', r'командир сторони', r'командир екіпаж', r'командир расч',
    r'пілот', r'оператор', r'оператор-навідник', r'наводчик', r'наводник', r'санитар',
    r'медик', r'гренадер', r'гранатометник', r'гранатомётчик', r'кулеметник', r'пулемётчик',
    r'стрілець', r'стрелок', r'старший стрілець', r'старший стрелок',
    r'механик-водій', r'механік-водій', r'механик-водитель', r'механік',
    r'снайпер'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

# ─── Reminder (optional) ──────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    # Класичне нагадування можна включити за потреби
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\n**Сбор VTG**")
                except Exception:
                    logger.exception("vtg_reminder send failed")

# ─── Embed builder for sessions ───────────────────────────────────────────────
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

# ─── Cleaning helpers ────────────────────────────────────────────────────────
def is_noise(s: str) -> bool:
    """
    Відсікає технічний шум: None/NULL/true/false, чисті цифри/капси,
    моди/прапори/сервісні токени: rhs_, _hide, flag_manager, MaleXXENG/PER/RUS,
    короткі службові: Uk, Honor, Army, Default, Platoon, Capture_1, DefaultRed, стандартні назви, тощо.
    """
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()
    noise_literals = {
        "none","null","true","false","army","default","platoon","standard","nochange","uk","ukr","honor",
        "capture_1","defaultred","standardred","everyone"
    }
    if low in noise_literals:
        return True
    # чисті числа
    if re.fullmatch(r'\d+', s):
        return True
    # капс/ідентифікатори
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True
    # мод/сервісні поля
    if ("rhs_" in low) or ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low) or ("door_" in low):
        return True
    # моделі/персонажі
    if s.startswith("Male") and ("ENG" in s or "PER" in s or "RUS" in s):
        return True
    return False

def strip_quotes_semicolons(s: str) -> str:
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'^[\'"]+|[\'"]+$', '', s)
    s = re.sub(r';+$', '', s)
    return s.strip()

def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    attrs = []
    for m in re.finditer(r'(?:value|description)\s*=\s*"([^"]+)"', s, flags=re.IGNORECASE):
        attrs.append(m.group(1))
    t_chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if attrs or t_chunks:
        combined = " ".join(attrs + t_chunks)
        combined = re.sub(r'<[^>]+>', ' ', combined)
        combined = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', combined)
        combined = re.sub(r'\s{2,}', ' ', combined).strip(' "\'').strip()
        return combined
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    return s

def looks_like_code_block(s: str) -> bool:
    if not s:
        return True
    if re.search(r'\b(condition|expression|init|compile|preprocessfilelinenumbers|thislist|playerSide|vehicle player)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'\\n|\\r|\\t|"\s*\n', s):
        return True
    if re.search(r'[{}()\[\];=<>!|&\\]', s):
            # якщо це майже одні символи
            if len(re.findall(r'[A-Za-zА-Яа-яЁёЇїІіЄєҐґ]', s)) < 5:
                return True
    return False

def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    s = re.sub(r'^\s*\d+\.\s*', '', s)
    s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
    s = s.strip(' "\'')
    return s

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    s = s.strip(" \t\n\r-–—")
    return s

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
                # prefer Cyrillic-rich or longer
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

def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("cp1251", errors="replace")

# ─── Core parser ─────────────────────────────────────────────────────────────
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Витягує (title, [slots...]) зі значень description="..." / value="...".
    Фільтрує технічний шум, збирає заголовки по '|' або контекстним ключовим словам.
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
    cur_title: Optional[str] = None
    cur_slots: List[str] = []
    rejected: List[str] = []

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
            else:
                rejected.append(s)
            continue

        # Короткі або технічні токени (ENG/MED/техніка) — трактувати як слот, якщо не шум
        if re.search(r'\b(ENG|MED|M113|M113A3|ZALA|Mavic|FPV|BMP|БМП|T-72|T-80|GAZ|ScanEagle|Орлан)\b', s, flags=re.IGNORECASE) or len(s.split()) <= 6:
            if not is_noise(s):
                cur_slots.append(clean_line_for_slot(s))
            continue

        # Інакше — або додаємо як слот до активного заголовка, або робимо заголовок
        if cur_title:
            if not is_noise(s):
                cur_slots.append(clean_line_for_slot(s))
        else:
            if len(s) > 10 and not is_noise(s):
                flush()
                cur_title = s
            else:
                if not is_noise(s):
                    cur_slots.append(clean_line_for_slot(s))

    flush()

    # Фолбек: якщо не знайдено груп — витягти усі нумеровані рядки як одне "Відділення"
    if not groups:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2) and not is_noise(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order(cleaned)
        if cleaned:
            groups = [(DEFAULT_TITLE, cleaned)]

    # Логування відкинутих рядків (необов'язково)
    try:
        if rejected:
            with open("parser_debug.log", "w", encoding="utf-8") as f:
                f.write("=== Rejected lines (first 200) ===\n")
                for r in rejected[:200]:
                    f.write(r.replace("\n", " ") + "\n")
    except Exception:
        logger.exception("Failed to write parser_debug.log")

    return groups

# ─── Команда: стоп ───────────────────────────────────────────────────────────
@bot.command(name="стоп", aliases=["stop"])
async def стоп(ctx: commands.Context):
    global _stop_sending_global, _stop_sending_by_channel
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    _stop_sending_global = True
    _stop_sending_by_channel[ctx.channel.id] = True
    await ctx.send("⏹️ Зупиняю відправку відділень...")

# ─── Команда: імпорт_sqm з аргументом 1-2 ───────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm", "імпорт_sqm_decoded", "import_sqm_decoded"])
async def імпорт_sqm(ctx: commands.Context, filter_id: str = None):
    """
    Обробляє вкладення з mission.sqm (текстовий).
    Якщо передано filter_id (наприклад "1-2"), повертає лише відділення з таким індексом у заголовку.
    """
    global _stop_sending_global, _stop_sending_by_channel

    # Перевірка прав (опціонально)
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")

    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть текстовий mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]

    # Дебаунс по message.id та attachment.id
    key = f"{ctx.message.id}:{attachment.id}"
    now = time.time()
    # cleanup old keys
    for k, t in list(_recent_imports.items()):
        if now - t > _RECENT_IMPORTS_TTL:
            _recent_imports.pop(k, None)
    if key in _recent_imports:
        return await ctx.send("⚠️ Ця команда вже обробляється (повторне надходження).")
    _recent_imports[key] = now

    _stop_sending_by_channel[ctx.channel.id] = False
    _stop_sending_global = False

    try:
        raw = await attachment.read()
        if isinstance(raw, (bytes, bytearray)):
            # Спробуємо UTF-8, потім CP1251
            try:
                sqm_text = raw.decode("utf-8")
            except Exception:
                sqm_text = raw.decode("cp1251", errors="replace")
        else:
            sqm_text = str(raw)
    except Exception as e:
        logger.exception("Failed to read attachment")
        _recent_imports.pop(key, None)
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    # Основний парсинг
    try:
        groups = extract_units_and_slots(sqm_text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # Фільтр по аргументу (наприклад, "1-2")
    if filter_id:
        groups = [g for g in groups if filter_id in (g[0] or "")]

    # Фолбек: якщо нічого не знайдено — спроба витягти всі нумеровані рядки
    if not groups and not filter_id:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', sqm_text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2) and not is_noise(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order([c for c in cleaned if c and not re.fullmatch(r'^[\W_]{1,5}$', c)])
        if cleaned:
            groups = [("Відділення", cleaned)]

    # Якщо все ще нічого — повертаємо одне повідомлення
    if not groups:
        logger.info("No groups found in attachment %s (message %s)", attachment.filename, ctx.message.id)
        _recent_imports.pop(key, None)
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # Відправка результатів — по одному блоку на групу
    sent = 0
    for title, slots in groups:
        if _stop_sending_global or _stop_sending_by_channel.get(ctx.channel.id):
            await ctx.send("⏹️ Відправка зупинена.")
            break
        title_line = re.sub(r'\s{2,}', ' ', title).strip()
        lines = [title_line] + slots
        out_text = "\n".join(lines)
        try:
            await ctx.send(f"```{out_text}```")
            sent += 1
        except Exception:
            # Розбити на частини, якщо дуже довго
            parts = out_text.splitlines()
            chunk = []
            for i, line in enumerate(parts):
                chunk.append(line)
                if (i + 1) % 40 == 0:
                    if _stop_sending_global or _stop_sending_by_channel.get(ctx.channel.id):
                        break
                    await ctx.send(f"```{chr(10).join(chunk)}```")
                    chunk = []
                    await asyncio.sleep(0)
            if chunk and not (_stop_sending_global or _stop_sending_by_channel.get(ctx.channel.id)):
                await ctx.send(f"```{chr(10).join(chunk)}```")
                sent += 1
        await asyncio.sleep(0.12)

    _stop_sending_by_channel[ctx.channel.id] = False
    _stop_sending_global = False
    _recent_imports.pop(key, None)
    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

# ─── UI: slot buttons and claim flow ────────────────────────────────────────
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
        ch_id = sess["channel_id"]
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message("⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True)
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        return await inter.response.send_message(f"⚠️ Цей слот зайнято {owner.mention}.", view=ClaimSlotView(self.sid, self.idx), ephemeral=True)

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

class ClaimSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(label="❗ Претендувати", style=discord.ButtonStyle.primary, custom_id=f"claim-slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        for s in sessions.values():
            if s["channel_id"] == sess["channel_id"] and user in s["owners"]:
                return await inter.response.send_message("⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True)
        key = (self.sid, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message("ℹ️ Ви вже подали заявку.", ephemeral=True)
        lst.append(user)
        await inter.response.send_message("✅ Заявка прийнята.", ephemeral=True)
        global request_counter
        request_counter += 1
        embed = discord.Embed(title=f"📝 Заявка #{request_counter}", description=sess["title"], color=discord.Color.orange())
        embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
        embed.add_field(name="Власник", value=(sess["owners"][self.idx].mention if sess["owners"][self.idx] else "Вільний"), inline=True)
        embed.add_field(name="Кандидат", value=user.mention, inline=False)
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID) if ADMIN_CHANNEL_ID else None
        if admin_ch:
            msg = await admin_ch.send(embed=embed)
            await msg.edit(view=ClaimDecisionView(self.sid, self.idx, user.id, msg.id))

class ClaimSlotView(View):
    def __init__(self, sid: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(sid, idx))

class DecisionModal(Modal):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int, accept: bool):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.sid = sid; self.idx = idx; self.claimant_id = claimant_id; self.admin_msg_id = admin_msg_id; self.accept = accept
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        key = (self.sid, self.idx)
        claimant = await bot.fetch_user(self.claimant_id)
        old_owner = sess["owners"][self.idx]
        reason = self.reason.value
        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)
        else:
            lst = claims.get(key, [])
            if claimant in lst:
                lst.remove(claimant)
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass
        try:
            if self.accept:
                await claimant.send(f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
                if old_owner and old_owner != claimant:
                    await old_owner.send(f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\nПричина: {reason}")
            else:
                await claimant.send(f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\nПричина: {reason}")
        except: pass
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID) if ADMIN_CHANNEL_ID else None
        if admin_ch:
            try:
                admin_msg = await admin_ch.fetch_message(self.admin_msg_id)
                await admin_msg.delete()
            except: pass
        await inter.response.send_message("✔️ Готово.", ephemeral=True)

class ClaimDecisionButton(Button):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int, accept: bool):
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(label=label, style=style, custom_id=f"dec-{tag}-{sid}-{idx}-{claimant_id}-{admin_msg_id}")
        self.sid = sid; self.idx = idx; self.claimant_id = claimant_id; self.admin_msg_id = admin_msg_id; self.accept = accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(self.sid, self.idx, self.claimant_id, self.admin_msg_id, self.accept)
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, False))

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
            except: pass
        try:
            await owner.send(f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
        except: pass
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

@bot.command(name="зняти")
async def зняти(ctx: commands.Context, session_msg_id: int):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:", view=RemoveSlotView(session_msg_id))

# ─── on_ready / on_message / service commands ─────────────────────────────────
@bot.event
async def on_ready():
    logger.info("[on_ready] %s", bot.user)
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
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))
    await bot.process_commands(message)

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

# ─── Run ─────────────────────────────────────────────────────────────────────
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
else:
    bot.run(TOKEN)
