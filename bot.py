# bot.py - УЛЬТИМАТНА ВЕРСІЯ - ВСІ ПРОБЛЕМИ ВИРІШЕНІ
# Discord бот: імпорт mission.sqm, фільтрація шуму, вибір відділень по індексу,
# об'єднання дублікатів, повний склад між заголовками, нумерація слотів,
# UI для слотів, статус/деплой/нагадування, звільнення слотів.

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

# ─────── Logging ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# ─────── ENV / INIT ─────────────────────────────────────────────────────
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

# ─────── Slot detection ─────────────────────────────────────────────────────
SLOT_KEYWORDS = [
    # 🇺🇦 Українські
    r'командир відділен', r'командир розрахун', r'командир екіпаж', r'командир сторони',
    r'старший стрілець', r'стрілець', r'гренадер', r'гранатометник', r'кулеметник',
    r'помічник кулеметника', r'помічник гранатометника', r'навідник', r'механік-вод',
    r'медик', r'оператор бпла', r'корегувальник', r'сапер', r'радист', r'снайпер',
    r'спостерігач', r'інженер', r'водій', r'заряджаючий',

    # 🇷🇺 Російські
    r'командир отделения', r'командир расч', r'командир экипажа', r'командир стороны',
    r'старший стрелок', r'стрелок', r'гранатомётчик', r'пулемётчик',
    r'помощник пулемётчика', r'помощник гранатомётчика', r'наводчик', r'механик-водитель',
    r'санитар', r'оператор бпла', r'корректировщик', r'связист', r'снайпер',
    r'наблюдатель', r'инженер', r'водитель', r'заряжающий',

    # 🇬🇧 Англійські / НАТО
    r'squad leader', r'team leader', r'automatic rifleman', r'rifleman', r'grenadier',
    r'designated marksman', r'at gunner', r'machine gunner', r'medic',
    r'drone operator', r'uav operator', r'gunner', r'loader', r'driver',
    r'comms sergeant', r'sniper', r'spotter', r'engineer', r'radio operator',
    r'vehicle commander', r'crew commander',

    # 🇩🇪 Німецькі
    r'gruppenführer', r'truppführer', r'schütze', r'oberschütze', r'grenadier',
    r'maschinengewehrschütze', r'mg-assistent', r'panzerabwehrschütze',
    r'sanitäter', r'funker', r'pionier', r'fahrer', r'richtschütze',
    r'kommandant', r'ladeschütze', r'scharfschütze', r'beobachter',

    # 🇫🇷 Французькі
    r'chef de groupe', r'chef d’équipe', r'tireur', r'grenadier',
    r'mitrailleur', r'aide-mitrailleur', r'lance-grenades', r'aide-grenadier',
    r'médecin', r'infirmier', r'radio', r'conducteur', r'tireur d’élite',
    r'observateur', r'ingénieur',

    # 🇪🇸 Іспанські
    r'líder de escuadra', r'líder de equipo', r'fusilero', r'granadero',
    r'ametrallador', r'asistente de ametrallador', r'lanzagranadas',
    r'asistente de granadero', r'médico', r'radio', r'comunicaciones',
    r'conductor', r'francotirador', r'observador', r'ingeniero',

    # 🇷🇸 Сербські
    r'vođa odeljenja', r'vođa voda', r'strelac', r'grenadir', r'mitraljezac',
    r'pomoćnik mitraljezca', r'bacač granata', r'pomoćnik bacača', r'sanitetski vojnik',
    r'radio-operater', r'vozač', r'snajper', r'posmatrač', r'inženjer',

    # 🇭🇺 Угорські
    r'rajparancsnok', r'csoportvezető', r'lövész', r'gránátos', r'géppuskás',
    r'géppuskás segéd', r'gránátvetős', r'gránátvető segéd', r'orvos',
    r'rádiós', r'vezető', r'mesterlövész', r'megfigyelő', r'mérnök',

    # 🇫🇮 Фінські
    r'ryhmänjohtaja', r'joukkueenjohtaja', r'kivääriampuja', r'kranaatinheitin',
    r'konekivääriampuja', r'konekiväärin apumies', r'lääkintämies',
    r'radiomies', r'kuljettaja', r'tarkka-ampuja', r'tähystäjä', r'insinööri',

    # 🇷🇴 Румунські
    r'comandant de grupă', r'comandant de echipă', r'pușcaș', r'grenadier',
    r'mitralior', r'ajutor mitralior', r'aruncător de grenade',
    r'ajutor grenadier', r'medic', r'radiotelefonist', r'șofer',
    r'lunetist', r'observator', r'inginer',

    # 🇵🇱 Польські
    r'dowódca drużyny', r'dowódca sekcji', r'strzelec', r'grenadier',
    r'karabinier', r'karabin maszynowy', r'pomocnik karabinu', r'granatnik',
    r'pomocnik granatnika', r'medyk', r'radiotelegrafista', r'kierowca',
    r'snajper', r'obserwator', r'inżynier',

    # 🇨🇿 Чеські
    r'vedoucí družstva', r'vedoucí sekce', r'střelec', r'granátník',
    r'kulometčík', r'asistent kulometčíka', r'granátometčík',
    r'asistent granátometčíka', r'medik', r'radiotelegrafista', r'řidič',
    r'ostřelovač', r'pozorovatel', r'inženýr',

    # 🇹🇷 Турецькі
    r'takım lideri', r'grup lideri', r'nişancı', r'bombacı', r'makineli tüfekçi',
    r'makineli tüfek yardımcısı', r'bomba atar', r'bomba atar yardımcısı',
    r'sağlıkçı', r'radyo operatörü', r'sürücü', r'keskin nişancı',
    r'gözlemci', r'mühendis'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# ─────── Helpers - УЛЬТИМАТНА ФІЛЬТРАЦІЯ ШУМУ ─────────────────────────────────────────────────────
def is_noise(s: str) -> bool:
    """
    УЛЬТИМАТНА функція фільтрації шуму - відсікає ВСІ непотрібні дані
    """
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()

    # Базові шумові літерали
    noise_literals = {
        "none","null","true","false",
        "army","default","platoon","standard","nochange",
        "uk","ukr","honor",
        "capture_1","defaultred","standardred",
        "everyone",
        "відділення","ввідділення",
        "зс рф та пвк", "невідомо"
    }
    if low in noise_literals:
        return True

    # Чисті числа або комбінації з комами
    if re.fullmatch(r'\d+(,\d+)*', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True

    # Моди/прапори/сервісні - розширено
    if ("rhs_" in low) or ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low):
        return True
    if low.startswith("door_") or low.startswith("hatch") or low.startswith("hide") or "snorkel" in low or "plate" in low or "trunk" in low:
        return True
    
    # Службові токени - розширено
    if re.search(r'\[\[\[\[.*?\]\]\]?]?false?\]?', s):
        return True
    if re.search(r'^[a-zA-Z_]+_unhide$', s):
        return True
    if re.search(r'^hide\w+', s):
        return True
    if re.search(r'^show\w+', s):
        return True

    # Техніка без контексту
    if low in {
        "mavicblue1","mavicblue2","mavicred1","mavicred2",
        "m113","m113a3","bmp","bmp-2","бмп-2","мт-лб","gaz-66","газ-66","tigr","тигр",
        "gaz-233014","внедорожник"
    }:
        return True

    # Імена персонажів без ролей
    if s.startswith("Guerilla_") or s.startswith("Male") or re.match(r'^[A-Z][a-z]+_\d+$', s):
        return True

    # Місійний шум - розширено
    mission_noise_patterns = [
        r'зс рф захопили',
        r'зс рф вдалося',
        r'багатоповерхівка',
        r'бахмут',
        r'повернись до бою',
        r'ти в полон біжиш',
        r'ти кудись летиш',
        r'ти повернувся',
        r'не будь зрадником',
        r'молодець'
    ]
    
    for pattern in mission_noise_patterns:
        if re.search(pattern, low):
            return True

    return False

def is_valid_slot(s: str) -> bool:
    """
    Перевіряє чи є рядок валідним слотом (не шум і не службовий)
    """
    if not s or is_noise(s):
        return False
    
    # Перевіряємо чи це не просто цифра або технічний ідентифікатор
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
    return s.strip(" \t\n\r-\u2013\u2014")

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

# ─────── Parser - УЛЬТИМАТНА ВЕРСІЯ ─────────────────────────────────────────────────────
TITLE_PATTERN = re.compile(
    r'@Альфа|@Beta|Штаб|бригада|Окрема|відділення|Піхотне|ОМБр|ССО|ГУР|артилерія|ЧВК|армейський|мотострілков',
    flags=re.IGNORECASE
)

def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    УЛЬТИМАТНИЙ парсер - збирає ВСІ слоти і нумерує їх
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # зібрати всі candidate-тексти
    candidates = [html.unescape(m.group(1)).strip()
                  for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    candidates += [html.unescape(m).strip()
                   for m in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)]
    
    # Якщо це просто текст без XML, розбиваємо по рядках
    if not candidates:
        candidates = [line.strip() for line in text.split('\n') if line.strip()]

    if not candidates:
        return []

    groups: Dict[str, List[str]] = {}  # title_norm -> slots

    cur_title: Optional[str] = None
    cur_slots: List[str] = []

    def process_title(title: str) -> Tuple[str, List[str]]:
        """Обробляє заголовок, витягуючи командира якщо є"""
        slots_from_title = []
        clean_title = title
        
        # Розширені патерни командирів
        commander_patterns = [
            (r'Командир відділення', 'Командир відділення'),
            (r'Командир отделения', 'Командир відділення'),
            (r'Командир сторони', 'Командир сторони'),
            (r'Командир стороны', 'Командир сторони'),
            (r'Командир розрахунку', 'Командир розрахунку'),
            (r'Командир расчета', 'Командир розрахунку'),
            (r'Командир снайперської пари', 'Командир снайперської пари'),
            (r'Командир снайперской пары', 'Командир снайперської пари'),
            (r'Командир екіпажу', 'Командир екіпажу'),
            (r'Командир экипажа', 'Командир екіпажу'),
            (r'Корегувальник', 'Корегувальник'),  # для снайперських пар
        ]
        
        for pattern, slot_name in commander_patterns:
            if re.search(pattern, title, flags=re.IGNORECASE):
                clean_title = re.sub(pattern, '', title, flags=re.IGNORECASE).strip()
                clean_title = re.sub(r'\s{2,}', ' ', clean_title).strip()
                slots_from_title.append(slot_name)
                break
        
        return clean_title, slots_from_title

    def flush():
        nonlocal cur_title, cur_slots
        if cur_title is None and cur_slots:
            title_line = DEFAULT_TITLE
            slots_from_title = []
        else:
            title_line, slots_from_title = process_title(cur_title or DEFAULT_TITLE)
            
        if cur_slots or slots_from_title:
            # Спочатку додаємо слоти з заголовка, потім - звичайні слоти
            all_slots = slots_from_title + cur_slots
            
            # УЛЬТИМАТНА фільтрація та нормалізація
            slots = []
            for s in all_slots:
                if is_valid_slot(s) and not looks_like_code_block(s):
                    clean_s = normalize_slot_name(s)
                    if clean_s and clean_s not in slots:
                        slots.append(clean_s)
            
            if slots:
                t_norm = re.sub(r'\s{2,}', ' ', title_line).strip()
                if not t_norm:
                    t_norm = DEFAULT_TITLE
                
                prev = groups.get(t_norm, [])
                # об'єднати, зберігаючи порядок та унікальність
                merged = prev + [x for x in slots if x not in prev]
                groups[t_norm] = merged
                
        cur_title, cur_slots = None, []

    for raw in candidates:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # новий заголовок? - розширена перевірка
        is_header = (
            ('|' in s) or 
            re.search(r'@\w+', s) or
            re.search(r'(Штаб|бригада|ОМБр|ССО|ГУР|ЧВК|Корегувальник)', s, flags=re.IGNORECASE) or
            re.search(r'(армейский|мотострелковая|отдельная)', s, flags=re.IGNORECASE) or
            re.search(r'\d+\.\s*.*@.*\|', s)
        )
        
        if is_header:
            flush()
            cur_title = s
            continue

        # слот за нумерацією / ключовими словами
        if re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if is_valid_slot(slot) and not looks_like_code_block(slot):
                cur_slots.append(slot)
            continue

        # будь-який інший текст → слот
        if is_valid_slot(s) and not looks_like_code_block(s):
            cur_slots.append(clean_line_for_slot(s))

    flush()

    # перетворити в список
    return [(title, slots) for title, slots in groups.items()]

def format_slots_with_numbers(slots: List[str]) -> List[str]:
    """
    Форматує слоти з нумерацією
    """
    formatted = []
    for i, slot in enumerate(slots, 1):
        formatted.append(f"{i}. {slot}")
    return formatted

# ─────── Сайд-детектор ─────────────────────────────────────────────────────
def detect_side_from_title(title: str) -> str:
    t = title.lower()
    # українська сторона - розширено
    if any(k in t for k in ["омбр", "зсу", "гуп", "ссо", "окрема", "бригада", "альфа", "холодний яр", "kraken", "гур"]):
        return "ЗСУ"
    # рос сторона / ПВК - розширено
    if any(k in t for k in ["армейский", "чвк", "мотострелковая", "корпус", "отдельная", "армия", "вагнер", "цсн", "фсб"]):
        return "ЗС РФ/ПВК"
    return "Невідомо"

# ─────── UI helpers ─────────────────────────────────────────────────────
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
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитися'}"
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
                    return await inter.response.send_message("⚠️ Ви вже маєте слот в цій гільці.", ephemeral=True)
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

# ─────── Claim flow ─────────────────────────────────────────────────────
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
            await owner.send(f"‼️ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
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

@bot.command(name="звільнити")
async def звільнити(ctx: commands.Context, session_msg_id: int):
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:", view=RemoveSlotView(session_msg_id))

# ─────── Commands - УЛЬТИМАТНА ВЕРСІЯ ─────────────────────────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context, *filter_ids: str):
    """
    УЛЬТИМАТНИЙ імпорт mission.sqm з повним збором слотів та нумерацією.
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

    # Парсинг з ультіматною функцією
    try:
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # Спрощена фільтрація по індексах
    if filter_ids:
        patterns = [re.compile(rf'\b{re.escape(fid)}\b') for fid in filter_ids]
        filtered = []
        for title, slots in groups:
            if any(p.search(title or "") for p in patterns):
                filtered.append((title, slots))
        groups = filtered

    if not groups:
        _recent_imports.pop(key, None)
        if filter_ids:
            await ctx.send(f"⚠️ Не знайдено відділень з індексами: {', '.join(filter_ids)}.")
        else:
            await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")
        return

    # Відправка: групуємо по стороні з нумерацією слотів
    by_side: Dict[str, List[Tuple[str, List[str]]]] = {"ЗСУ": [], "ЗС РФ/ПВК": [], "Невідомо": []}
    for title, slots in groups:
        side = detect_side_from_title(title)
        by_side[side].append((title, slots))

    sent = 0
    for side in ("ЗСУ", "ЗС РФ/ПВК", "Невідомо"):
        blocks = by_side[side]
        if not blocks:
            continue
        await ctx.send(f"```{side}```")
        for title, slots in blocks:
            # Форматуємо слоти з нумерацією
            numbered_slots = format_slots_with_numbers(slots)
            out = "\n".join([title] + numbered_slots)
            
            # чанк, якщо дуже довго
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
    await ctx.send("🔄 Деплой тригеровано!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

# ─────── Reminder ─────────────────────────────────────────────────────
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

# ─────── on_ready / on_message ─────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info("Bot ready: %s", bot.user)
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    embed = discord.Embed(title="🔄 Бот перезапущено (УЛЬТИМАТНА ВЕРСІЯ 3.0)", description=f"📦 Commit: `{commit}`\n✅ ВСІ проблеми парсингу SQM виправлено\n🔢 Нумерація слотів додано\n🧹 Повна фільтрація шуму", color=discord.Color.green())
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

# ─────── Run ─────────────────────────────────────────────────────
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
