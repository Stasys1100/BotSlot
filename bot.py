# bot.py — ВИПРАВЛЕНА ВЕРСІЯ v3 - ФІНАЛЬНА
# Виправлення всіх критичних помилок:
# 1. Видалення "@" та "\\" з назв відділень та слотів
# 2. Видалення командира з заголовка і додавання його як перший слот
# 3. Усунення дублювання "MED", зброї, та відділень
# 4. Очищення роздільників "| |"

import os
import re
import html
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

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# Env
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
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0

# Slot keywords (multilingual)
SLOT_KEYWORDS = [
    r'командир відділен', r'командир розрахун', r'командир екіпаж', r'командир сторони', r'командир взводу',
    r'старший стрілець', r'стрілець', r'гренадер', r'гранатометник', r'кулеметник',
    r'помічник кулеметника', r'помічник гранатометника', r'навідник', r'оператор-навідник',
    r'механік-вод', r'медик', r'санітар', r'оператор бпла', r'корегувальник',
    r'снайпер', r'радист', r'інженер', r'водій', r'заряджаючий', r'пілот',
    r'командир отделения', r'командир расчёта', r'старший стрелок', r'стрелок', r'гранатомётчик', r'пулемётчик',
    r'помощник пулемётчика', r'помощник гранатомётчика', r'наводчик-оператор', r'наводчик', r'механик-водитель',
    r'второй-пилот', r'пилот',
    r'squad leader', r'team leader', r'rifleman', r'grenadier', r'machine gunner', r'medic',
    r'drone operator', r'gunner', r'loader', r'driver', r'sniper', r'at gunner', r'comms sergeant',
    r'komandas vadītājs', r'pieredzējis šāvējs', r'ložmetējnieks', r'ložmetēja asistents',
    r'granātnieks', r'granātnieka asistents', r'mediķis', r'šāvējs'
]

SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)
TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# Helpers
def is_noise(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()
    noise_literals = {"none","null","true","false","army","default","platoon","standard","nochange","uk","ukr","honor","everyone","відділення","ввідділення","зс рф та пвк","невідомо"}
    if low in noise_literals:
        return True
    if re.fullmatch(r'\d+(,\d+)*', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True
    if ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low) or ("rhs_" in low):
        return True
    if re.search(r'^(crate|wood|door|hide|show)_[\w\-]+(_unhide)?$', low):
        return True
    if re.search(r'\[\[\[\[.*?\]\]\]?]?false?\]?', s):
        return True
    if low in {"mavicblue1","mavicblue2","mavicred1","mavicred2","m113","m113a3","bmp","bmp-2","бмп-2","мт-лб","gaz-66","газ-66","tigr","тигр","gaz-233014","внедорожник"}:
        return True
    if s.startswith("Guerilla_") or s.startswith("Male") or re.match(r'^[A-Z][a-z]+_\d+$', s):
        return True
    event_noise = [r'зс рф захопили', r'зс рф змогли', r'зс рф вдалося', r'багатоповерхівка', r'бахмут', r'повернись до бою', r'ти в полон біжиш', r'ти кудись летиш', r'ти повернувся', r'не будь зрадником', r'молодець']
    if any(re.search(p, low) for p in event_noise):
        return True
    if re.fullmatch(r'[\|\s]+', s):
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
    if re.search(r'[{}()\[\];=<>!|&\\]', s) and len(re.findall(r'[A-Za-zА-Яа-яЁёЏїІіЄєҐґ]', s)) < 5:
        return True
    return False

def normalize_pipes(s: str) -> str:
    # Remove empty pipe sections
    s = re.sub(r'\|\s*\|', '|', s)
    s = re.sub(r'\s*\|\s*', ' | ', s)
    s = re.sub(r'(?:\s*\|\s*){2,}', ' | ', s)
    s = re.sub(r'^\s*\|\s*', '', s)
    s = re.sub(r'\s*\|\s*$', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s

def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
    
    # Remove backslashes used as separators
    s = re.sub(r'\\', ' ', s)
    
    # Remove language tags
    s = re.sub(r'\s*\|\s*(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO|LV)\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO|LV)\b', '', s, flags=re.IGNORECASE)
    
    # Normalize MED variants
    s = re.sub(r'\b(MED|МЕД|Медик|Mediķis|Medic|Санітар|Санитар|Польовий медик)\b', 'MED', s, flags=re.IGNORECASE)
    
    # Remove duplicate MED patterns
    s = re.sub(r'\bMED\s*\\?\s*MED\b', 'MED', s, flags=re.IGNORECASE)
    s = re.sub(r'\bMED\s+MED\b', 'MED', s, flags=re.IGNORECASE)
    
    # Remove duplicate weapons in parentheses: (G36A3) (G36A3\AG-40) -> (G36A3\AG-40)
    # Find all weapons in parentheses
    weapons = re.findall(r'\([^)]+\)', s)
    if len(weapons) > 1:
        # Keep only the most detailed one (longest)
        longest_weapon = max(weapons, key=len)
        # Remove all weapons
        for w in weapons:
            s = s.replace(w, '', 1)
        # Add back the longest one
        s = s.strip() + ' ' + longest_weapon
    
    s = normalize_pipes(s)
    
    # Ensure MED is separated by pipe if it's at the end
    s = re.sub(r'\s+MED\s*$', ' | MED', s)
    
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'')
    
    return s

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    return s.strip(" \t\n\r-\u2013\u2014")

def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("cp1251", errors="replace")

# Title cleaning - IMPROVED v3
def strip_title_prefixes(title: str) -> str:
    t = (title or "").strip()
    
    # Remove @ tokens anywhere
    t = re.sub(r'@[^\s|]+', '', t)
    
    # Remove backslashes
    t = re.sub(r'\\+', ' ', t)
    
    # Remove leading numbers
    t = re.sub(r'^\s*\d+\s*[\.\:]\s*', '', t)
    
    # Remove language codes
    t = re.sub(r'^\s*(ENG|RU|UA|UKR|PL|DE|FR|ES|TR|CZ|FI|HU|RO|LV)\s*(\|\s*)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^[A-Za-zА-Яа-я]\s*\|\s*', '', t)
    
    t = re.sub(r'^\s*\|\s*[A-Z]{2,}\s*', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' |@\\')
    t = re.sub(r'\s*\|\s*$', '', t)
    
    return t

def extract_commander_and_weapon_from_title(title: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Extract commander role and weapon from title.
    Returns: (clean_title, commander_slot, weapon)
    """
    t = (title or "").strip()
    commander_slot = None
    weapon = None
    
    # Commander patterns
    commander_patterns = [
        (r'Komandas vadītājs', 'Командир відділення'),
        (r'Командир взводу', 'Командир взводу'),
        (r'Командир відділення', 'Командир відділення'),
        (r'Командир отделения', 'Командир відділення'),
        (r'Командир расчёта', 'Командир розрахунку'),
        (r'Командир сторони', 'Командир сторони'),
        (r'Командир стороны', 'Командир сторони'),
        (r'Командир розрахун', 'Командир розрахунку'),
        (r'Командир расч', 'Командир розрахунку'),
        (r'Командир екіпаж', 'Командир екіпажу'),
        (r'Командир экипаж', 'Командир екіпажу'),
        (r'Squad Leader', 'Squad Leader'),
        (r'Crew Commander', 'Crew Commander'),
        (r'Vehicle Commander', 'Vehicle Commander'),
    ]
    
    # Try to find commander with weapon: "Commander (Weapon)"
    for pat, slot_name in commander_patterns:
        # Pattern with weapon
        m = re.search(rf'{pat}\s*\(([^)]+)\)', t, flags=re.IGNORECASE)
        if m:
            commander_slot = slot_name
            weapon = m.group(1).strip()
            # Remove commander and weapon from title
            t = re.sub(rf'{pat}\s*\([^)]+\)', '', t, flags=re.IGNORECASE)
            break
        
        # Pattern without weapon
        m2 = re.search(rf'{pat}(?:\s|$)', t, flags=re.IGNORECASE)
        if m2:
            commander_slot = slot_name
            # Remove commander from title
            t = re.sub(rf'{pat}', '', t, flags=re.IGNORECASE)
            break
    
    # Clean up
    t = strip_title_prefixes(t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' |@\\')
    
    return t or DEFAULT_TITLE, commander_slot, weapon

def process_title_final(title: str) -> Tuple[str, List[str], Optional[str]]:
    """
    Process title to extract clean title, commander slot, and weapon.
    """
    clean_title, commander_slot, weapon = extract_commander_and_weapon_from_title(title)
    
    slots_from_title: List[str] = []
    if commander_slot:
        slots_from_title.append(commander_slot)
    
    return clean_title, slots_from_title, weapon

# Canonicalization for dedupe
def canonical_slot_for_compare(s: str) -> str:
    if not s:
        return ""
    t = s
    # Remove weapons
    t = re.sub(r'\([^)]*\)', '', t)
    # Remove MED
    t = re.sub(r'\b\|\s*MED\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bMED\b', '', t, flags=re.IGNORECASE)
    # Remove special chars
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

# Parser - IMPROVED v3
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    candidates = [html.unescape(m.group(1)).strip()
                  for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    candidates += [html.unescape(m).strip()
                   for m in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)]
    if not candidates:
        candidates = [line.strip() for line in text.split('\n') if line.strip()]
    if not candidates:
        return []

    groups: Dict[str, List[str]] = {}
    cur_title: Optional[str] = None
    cur_slots: List[str] = []

    def flush():
        nonlocal cur_title, cur_slots
        if cur_title is None and not cur_slots:
            return
            
        title_line, slots_from_title, weapon = process_title_final(cur_title or DEFAULT_TITLE)

        if cur_slots or slots_from_title:
            all_slots = slots_from_title + cur_slots
            slots: List[str] = []
            
            for s in all_slots:
                if is_valid_slot(s) and not looks_like_code_block(s):
                    clean_s = normalize_slot_name(clean_line_for_slot(s))
                    if clean_s:
                        slots.append(clean_s)

            # Add weapon to first slot (commander) if weapon exists and slots exist
            if weapon and slots:
                # Check if weapon not already in first slot
                if not re.search(r'\([^)]*\)', slots[0]):
                    slots[0] = f"{slots[0]} ({weapon})"

            if slots:
                t_norm = strip_title_prefixes(title_line) or DEFAULT_TITLE
                
                # Deduplicate by canonical form
                key_title = canonical_title_for_compare(t_norm)
                key_slots = tuple(canonical_slot_for_compare(x) for x in slots)
                canonical_key = (key_title, key_slots)

                # Check if this exact squad already exists
                exists = False
                for existing_title in list(groups.keys()):
                    if canonical_title_for_compare(existing_title) == key_title:
                        existing_slots = groups[existing_title]
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

        cur_title, cur_slots = None, []

    for raw in candidates:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # Check if this is a header line
        has_index = re.search(r'\b(Альфа|Alpha)\s*\d+-\d+\b', s, flags=re.IGNORECASE) is not None
        is_header = ('|' in s and has_index) and not (re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s))

        if is_header:
            flush()
            cur_title = s
            continue

        # Check if this is a slot line
        if re.match(r'^\s*\d+\.\s*', s) or TRIGGER_RE.match(s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if is_valid_slot(slot) and not looks_like_code_block(slot):
                cur_slots.append(slot)
            continue

        # Otherwise treat as potential slot
        if is_valid_slot(s) and not looks_like_code_block(s):
            cur_slots.append(clean_line_for_slot(s))

    flush()
    return [(title, slots) for title, slots in groups.items()]

# Slot numbering
def format_slots_with_numbers(slots: List[str]) -> List[str]:
    if not slots:
        return []
    
    # Parse existing numbers
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
    
    # If we have explicit numbers, use them
    if numbers:
        min_num = min(numbers)
        # If first slot has no number, assign it
        if parsed[0][0] is None:
            assign = 1
            used = set(numbers)
            while assign in used:
                assign += 1
            parsed[0] = (assign, parsed[0][1])
            numbers.append(assign)
        
        result = []
        used_nums = set(numbers)
        next_num = 1
        
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
    
    # No explicit numbers, just number sequentially from 1
    return [f"{i+1}. {slot.strip()}" for i, slot in enumerate(slots)]

# UI helpers
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

# Claim flow
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

# Output builder
async def send_groups(ctx: commands.Context, grouped: Dict[str, List[Tuple[str, List[str]]]]):
    sent_keys = set()
    sent = 0
    all_blocks: List[Tuple[str, List[str]]] = []
    for blocks in grouped.values():
        all_blocks.extend(blocks)
    
    for title, slots in all_blocks:
        # Create canonical key for deduplication
        key = (canonical_title_for_compare(title), tuple(canonical_slot_for_compare(x) for x in slots))
        if key in sent_keys:
            continue
        sent_keys.add(key)
        
        numbered = format_slots_with_numbers(slots)
        out = "\n".join([title] + numbered)
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
        await asyncio.sleep(0.06)
    return sent

# Command: !слоти
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
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    normalized: Dict[str, List[str]] = {}
    seen_canonical: set = set()

    for title, slots in groups:
        t_clean, slots_from_title, _ = process_title_final(title)
        t_clean = strip_title_prefixes(t_clean or DEFAULT_TITLE)
        all_slots = slots_from_title + slots
        final_slots: List[str] = []
        
        for s in all_slots:
            s2 = clean_line_for_slot(s)
            if s2 and not is_noise(s2) and not looks_like_code_block(s2):
                final_slots.append(normalize_slot_name(s2))

        # Deduplicate
        key_title = canonical_title_for_compare(t_clean)
        key_slots = tuple(canonical_slot_for_compare(x) for x in final_slots)
        canonical_key = (key_title, key_slots)

        if canonical_key in seen_canonical:
            continue

        seen_canonical.add(canonical_key)
        
        if t_clean in normalized:
            idx = 2
            new_key = f"{t_clean} ({idx})"
            while new_key in normalized:
                idx += 1
                new_key = f"{t_clean} ({idx})"
            normalized[new_key] = final_slots
        else:
            normalized[t_clean] = final_slots

    if filter_ids:
        pats = [re.compile(rf'\b{re.escape(fid)}\b', flags=re.IGNORECASE) for fid in filter_ids]
        filtered: Dict[str, List[str]] = {}
        for t, sl in normalized.items():
            title_for_match = strip_title_prefixes(t)
            if any(p.search(title_for_match) for p in pats):
                filtered[t] = sl
        normalized = filtered

    if not normalized:
        _recent_imports.pop(key, None)
        if filter_ids:
            return await ctx.send(f"⚠️ Не знайдено відділень з індексами: {', '.join(filter_ids)}.")
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    by_side_like: Dict[str, List[Tuple[str, List[str]]]] = {"all": []}
    for t, sl in normalized.items():
        by_side_like["all"].append((t, sl))

    sent = await send_groups(ctx, by_side_like)
    _recent_imports.pop(key, None)
    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

# Admin commands
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
    await ctx.send("🔄 Деплой тригеровано!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    try:
        commit = subprocess.getoutput("git rev-parse --short=7 HEAD")
    except Exception:
        commit = "unknown"
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\n**Збір VTG**")
                except Exception:
                    logger.exception("vtg_reminder send failed")

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
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return
    if "запис слот" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слот" in txt.lower() or "everyone" in txt.lower():
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

# Run
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
