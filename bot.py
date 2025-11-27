# bot.py — ОСТАННЯ ВЕРСІЯ (фікси: ширше визначення заголовків, покращений фільтр 1-1, жорстка дедуплікація)
import os
import re
import html
import time
import subprocess
import logging
from typing import List, Tuple, Dict, Optional
from zoneinfo import ZoneInfo
from collections import OrderedDict

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

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

DEFAULT_TITLE = "Відділення"
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0

sessions: Dict[int, dict] = {}
processed_messages: set[int] = set()

# Slot keywords (multilingual)
SLOT_KEYWORDS = [
    r'командир відділен', r'командир розрахун', r'командир екіпаж', r'командир сторони',
    r'старший стрілець', r'стрілець', r'гренадер', r'гранатометник', r'кулеметник',
    r'помічник кулеметника', r'помічник гранатометника', r'навідник', r'оператор-навідник',
    r'механік-вод', r'медик', r'санітар', r'оператор бпла', r'корегувальник',
    r'снайпер', r'радист', r'інженер', r'водій', r'заряджаючий',
    r'командир отделения', r'старший стрелок', r'стрелок', r'гранатомётчик', r'пулемётчик',
    r'squad leader', r'team leader', r'rifleman', r'grenadier', r'machine gunner', r'medic',
    r'drone operator', r'gunner', r'loader', r'driver', r'sniper'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)
TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# ---------- Helpers ----------
def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("cp1251", errors="replace")

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

def normalize_pipes(s: str) -> str:
    s = re.sub(r'\s*\|\s*', ' | ', s)
    s = re.sub(r'(?:\s*\|\s*){2,}', ' | ', s)
    s = re.sub(r'^\s*\|\s*', '', s)
    s = re.sub(r'\s*\|\s*$', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s

def is_noise(s: str) -> bool:
    if not s:
        return True
    s = s.strip()
    low = s.lower()
    noise_literals = {"none","null","true","false","army","default","platoon","standard","nochange","uk","ukr","honor","everyone","відділення","ввідділення","зс рф та пвк","невідомо"}
    if low in noise_literals:
        return True
    if re.fullmatch(r'\d+(,\d+)*', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True
    if re.fullmatch(r'[\|\s]+', s):
        return True
    if re.search(r'^(crate|wood|door|hide|show)_[\w\-]+', low):
        return True
    return False

def looks_like_code_block(s: str) -> bool:
    if not s:
        return True
    if re.search(r'\b(condition|expression|init|compile|thislist|playerSide)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'\\n|\\r|\\t', s):
        return True
    if re.search(r'[{}()\[\];=<>!|&\\]', s) and len(re.findall(r'[A-Za-zА-Яа-яЁёЇїІіЄєҐґ]', s)) < 5:
        return True
    return False

def is_valid_slot(s: str) -> bool:
    if not s or is_noise(s):
        return False
    if re.fullmatch(r'^\d+$', s.strip()):
        return False
    if re.fullmatch(r'^[A-Z_]+$', s.strip()):
        return False
    return True

# ---------- Title / weapon extraction ----------
def strip_title_prefixes(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r'@[^|\n]+', '', t)  # remove @... tokens
    t = re.sub(r'^\s*\d+\s*[\.\:]\s*', '', t)
    t = re.sub(r'^\s*(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO)\s*(\|\s*)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^\s*\|\s*[A-Z]{2,}\s*', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' |')
    t = re.sub(r'\s*\|\s*$', '', t)
    return t

def extract_leading_weapon_and_strip(title: str) -> Tuple[str, Optional[str]]:
    t = (title or "").strip()
    m_sep = re.search(r'(@|\|)', t)
    if m_sep:
        left = t[:m_sep.start()]
        m_par = re.search(r'\([^\)]*\)\s*$', left)
        if m_par:
            weapon = m_par.group(0).strip()
            rest = (t[:m_par.start()] + t[m_sep.start():]).strip()
            rest = re.sub(r'^[\s@|:,-]+', '', rest).strip()
            return rest, weapon
    m = re.match(r'^\s*(\([^\)]+\))\s*(?:@|\||\b(Альфа|Alpha)\b)?', t)
    if m and m.group(1):
        weapon = m.group(1).strip()
        rest = t.replace(m.group(1), '').strip()
        rest = re.sub(r'^\s*@\S+', '', rest).strip()
        return rest, weapon
    m2 = re.match(r'^\s*([A-Za-z0-9\-\s\/\\\+]+?)\s*(?:@|\|)\s*(.+)$', t)
    if m2:
        weapon = m2.group(1).strip()
        rest = m2.group(2).strip()
        if re.search(r'\b(Альфа|Alpha)\b', weapon, flags=re.IGNORECASE):
            return t, None
        return rest, weapon
    return t, None

def process_title_final(title: str) -> Tuple[str, List[str], Optional[str]]:
    rest, weapon = extract_leading_weapon_and_strip(title)
    clean = strip_title_prefixes(rest)
    slots_from_title: List[str] = []
    commander_patterns = [
        (r'Командир відділення', 'Командир відділення'),
        (r'Командир отделения', 'Командир відділення'),
        (r'Командир сторони', 'Командир сторони'),
        (r'Командир стороны', 'Командир сторони'),
        (r'Командир розрахун', 'Командир розрахунку'),
        (r'Командир расч', 'Командир розрахунку'),
        (r'Командир екіпаж', 'Командир екіпажу'),
        (r'Squad Leader', 'Squad Leader'),
        (r'Crew Commander', 'Crew Commander'),
    ]
    for pat, slot_name in commander_patterns:
        if re.search(pat, clean, flags=re.IGNORECASE):
            clean = re.sub(pat, '', clean, flags=re.IGNORECASE).strip()
            slots_from_title.append(slot_name)
            break
    clean = re.sub(r'\s{2,}', ' ', clean).strip(' |')
    return clean or DEFAULT_TITLE, slots_from_title, weapon

# ---------- Canonicalization ----------
def canonical_slot_for_compare(s: str) -> str:
    if not s:
        return ""
    t = s
    t = re.sub(r'\([^)]*\)', '', t)
    t = re.sub(r'\b\|\s*MED\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bMED\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^A-Za-z0-9\u0400-\u04FF\s\-]', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip().lower()
    return t

def canonical_title_for_compare(t: str) -> str:
    if not t:
        return ""
    x = strip_title_prefixes(t)
    x = re.sub(r'\([^)]*\)', '', x)
    x = re.sub(r'[^A-Za-z0-9\u0400-\u04FF\s\-]', '', x)
    x = re.sub(r'\s{2,}', ' ', x).strip().lower()
    return x

# ---------- Parser ----------
def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'@[\w\-\u0400-\u04FF]+(?:\s*\d+[-–—]?\d*)?', '', s)  # remove inline @ markers
    s = re.sub(r'(\|\s*){2,}', '|', s)
    s = re.sub(r'\s+\|\s*$', '', s)
    s = re.sub(r'\s+\|\s*(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO)\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO)\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\b(MED|МЕД|Медик|Польовий медик)\b', 'MED', s, flags=re.IGNORECASE)
    s = normalize_pipes(s)
    s = re.sub(r'\s+MED$', ' | MED', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'')
    return s

def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Collect candidates but avoid duplicates at source (same string twice)
    seen_candidates = OrderedDict()
    for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE):
        val = html.unescape(m.group(1)).strip()
        if val:
            seen_candidates[val] = None
    for m in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL):
        val = html.unescape(m).strip()
        if val:
            seen_candidates[val] = None

    # fallback: split lines but dedupe identical lines
    if not seen_candidates:
        for line in text.splitlines():
            ln = line.strip()
            if ln:
                seen_candidates[ln] = None

    candidates = list(seen_candidates.keys())
    if not candidates:
        return []

    groups: Dict[str, List[str]] = OrderedDict()
    cur_title: Optional[str] = None
    cur_slots: List[str] = []
    pending_weapon: Optional[str] = None

    def flush():
        nonlocal cur_title, cur_slots, pending_weapon
        if cur_title is None and cur_slots:
            title_line, slots_from_title, weapon = DEFAULT_TITLE, [], None
        else:
            title_line, slots_from_title, weapon = process_title_final(cur_title or DEFAULT_TITLE)

        if pending_weapon and not weapon:
            weapon = pending_weapon

        if cur_slots or slots_from_title:
            all_slots = slots_from_title + cur_slots
            slots: List[str] = []
            for s in all_slots:
                if is_valid_slot(s) and not looks_like_code_block(s):
                    clean_s = normalize_pipes(clean_line_for_slot(s))
                    if clean_s:
                        slots.append(clean_s)

            if weapon:
                w = re.sub(r'^\(|\)$', '', weapon).strip()
                if w:
                    if slots:
                        if w not in slots[0]:
                            slots[0] = f"{slots[0]} ({w})"
                    else:
                        slots.append(w)

            if slots:
                t_norm = strip_title_prefixes(title_line) or DEFAULT_TITLE
                key_title = canonical_title_for_compare(t_norm)
                key_slots = tuple(canonical_slot_for_compare(x) for x in slots)

                # dedupe by canonical key
                exists = False
                for existing_title, existing_slots in list(groups.items()):
                    if canonical_title_for_compare(existing_title) == key_title:
                        existing_canonical = tuple(canonical_slot_for_compare(x) for x in existing_slots)
                        if existing_canonical == key_slots:
                            exists = True
                            break

                if not exists:
                    base = t_norm
                    if base in groups:
                        idx = 2
                        new_key = f"{base} ({idx})"
                        while new_key in groups:
                            idx += 1
                            new_key = f"{base} ({idx})"
                        groups[new_key] = slots
                    else:
                        groups[base] = slots

        cur_title, cur_slots, pending_weapon = None, [], None

    for raw in candidates:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # broader header detection: '|' or common unit words
        is_header = ('|' in s or re.search(r'\b(Battalion|Regiment|Brigade|Squad|Infantry|Mechanized|Armored|M113|BMP|T-34|M2|Reserve|Company)\b', s, flags=re.IGNORECASE)) and not (re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s))

        if is_header:
            flush()
            cur_title = s
            continue

        if cur_title and not cur_slots:
            if re.fullmatch(r'[\(\)A-Za-z0-9\-\s\/\\\+]{2,100}', s) and not SLOT_RE.search(s) and len(s) <= 100:
                pending_weapon = s
                continue

        if re.match(r'^\s*\d+\.\s*', s) or TRIGGER_RE.match(s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if is_valid_slot(slot) and not looks_like_code_block(slot):
                cur_slots.append(slot)
            continue

        if is_valid_slot(s) and not looks_like_code_block(s):
            cur_slots.append(clean_line_for_slot(s))

    flush()
    return [(title, slots) for title, slots in groups.items()]

# ---------- Build canonical map (strong dedupe) ----------
def build_canonical_map(parsed: List[Tuple[str, List[str]]]) -> "OrderedDict[Tuple[str, Tuple[str,...]], Tuple[str, List[str]]]":
    cmap: "OrderedDict[Tuple[str, Tuple[str,...]], Tuple[str, List[str]]]" = OrderedDict()
    seen_display: set = set()
    for title, slots in parsed:
        display_title = strip_title_prefixes(title) or DEFAULT_TITLE
        canonical_title = canonical_title_for_compare(display_title)
        canonical_slots = tuple(canonical_slot_for_compare(s) for s in slots)
        key = (canonical_title, canonical_slots)
        if key in cmap:
            continue
        display_key = (display_title, tuple(slots))
        if display_key in seen_display:
            continue
        seen_display.add(display_key)
        cmap[key] = (display_title, slots)
    return cmap

# ---------- Numbering and output ----------
def format_slots_with_numbers(slots: List[str]) -> List[str]:
    if not slots:
        return []
    numbers = []
    parsed = []
    for s in slots:
        m = re.match(r'^\s*(\d+)\s*[\.\:\)]\s*(.+)$', s)
        if m:
            num = int(m.group(1))
            numbers.append(num)
            parsed.append((num, m.group(2).strip()))
        else:
            parsed.append((None, s.strip()))
    if numbers:
        min_num = min(numbers)
        if parsed[0][0] is None:
            assign = max(1, min_num - 1)
            used = set(numbers)
            if assign in used:
                i = 1
                while i in used:
                    i += 1
                assign = i
            parsed[0] = (assign, parsed[0][1])
            numbers.append(assign)
            numbers.sort()
        result = []
        used_nums = set(n for n in numbers)
        next_num = min_num
        for num, text in parsed:
            if num is not None:
                result.append(f"{num}. {text}")
            else:
                while next_num in used_nums:
                    next_num += 1
                result.append(f"{next_num}. {text}")
                used_nums.add(next_num)
                next_num += 1
        return result
    return [f"{i+1}. {slot.strip()}" for i, slot in enumerate(slots, 1)]

def matches_filter(title: str, fid: str) -> bool:
    if not fid:
        return False
    t = title or ""
    t_norm = re.sub(r'[\s–—]+', ' ', t)
    fid_norm = fid.strip()

    # direct substring
    if re.search(re.escape(fid_norm), t_norm, flags=re.IGNORECASE):
        return True

    # compact compare (remove hyphens/spaces)
    fid_comp = re.sub(r'[-–—\s]+', '', fid_norm)
    t_comp = re.sub(r'[-–—\s]+', '', t_norm)
    if fid_comp and fid_comp.lower() in t_comp.lower():
        return True

    # if filter looks like "1-1" or "1:1" or "1 1", try numeric matching:
    nums = re.findall(r'\d+', fid_norm)
    if nums:
        # match any of the numbers in title (e.g., "1st", "1", "67th", "M113A3" contains digits too)
        title_nums = re.findall(r'\d+', t_norm)
        # if any numeric token matches any numeric token from fid, accept
        for n in nums:
            if any(n == tn or tn.startswith(n) or tn.endswith(n) for tn in title_nums):
                return True
        # also try ordinal forms: "1st" etc.
        for n in nums:
            if re.search(r'\b' + re.escape(n) + r'(st|nd|rd|th)?\b', t_norm, flags=re.IGNORECASE):
                return True

    # match by words: all words in fid appear in title (useful for "Reserve squad")
    words = [w for w in re.split(r'[\s\|,]+', fid_norm) if w]
    if words and all(re.search(re.escape(w), t_norm, flags=re.IGNORECASE) for w in words):
        return True

    return False

async def send_groups(ctx: commands.Context, canonical_map: "OrderedDict[Tuple[str, Tuple[str,...]], Tuple[str, List[str]]]"):
    sent = 0
    for (canon_title, canon_slots), (display_title, slots) in canonical_map.items():
        numbered = format_slots_with_numbers(slots)
        out = "\n".join([display_title] + numbered)
        lines = out.splitlines()
        chunk = []
        for i, line in enumerate(lines, 1):
            chunk.append(line)
            if i % 40 == 0:
                await ctx.send(f"```{chr(10).join(chunk)}```")
                chunk = []
                await asyncio.sleep(0)
        if chunk:
            await ctx.send(f"```{chr(10).join(chunk)}```")
        sent += 1
        await asyncio.sleep(0.05)
    return sent

# ---------- Discord UI / Commands ----------
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
            await owner.send(f"‼️ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
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

@bot.command(name="звільнити")
async def звільнити(ctx: commands.Context, session_msg_id: int):
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:", view=RemoveSlotView(session_msg_id))

@bot.command(name="слоти", aliases=["імпорт_sqm", "import_sqm"])
async def слоти(ctx: commands.Context, *filter_ids: str):
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
        return await ctx.send("⚠️ Ця команда вже обробляється (повтор).")
    _recent_imports[key] = now
    try:
        raw = await att.read()
        text = decode_bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as e:
        _recent_imports.pop(key, None)
        logger.exception("Failed to read attachment")
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")
    try:
        parsed = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        parsed = []
    canonical_map = build_canonical_map(parsed)
    if filter_ids:
        filtered_map = OrderedDict()
        for key, (display_title, slots) in canonical_map.items():
            title_for_match = strip_title_prefixes(display_title)
            if any(matches_filter(title_for_match, fid) for fid in filter_ids):
                filtered_map[key] = (display_title, slots)
        canonical_map = filtered_map
    if not canonical_map:
        _recent_imports.pop(key, None)
        if filter_ids:
            return await ctx.send(f"⚠️ Не знайдено відділень з індексами: {', '.join(filter_ids)}.")
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")
    sent = await send_groups(ctx, canonical_map)
    _recent_imports.pop(key, None)
    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

@bot.event
async def on_ready():
    logger.info("Bot ready: %s", bot.user)
    try:
        commit = subprocess.getoutput("git rev-parse --short=7 HEAD")
    except Exception:
        commit = "unknown"
    embed = discord.Embed(title="🔄 Бот перезапущено", description=f"📦\nCommit: `{commit}`", color=discord.Color.green())
    for guild in bot.guilds:
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages, guild.text_channels)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                logger.exception("Failed to announce restart")

if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
