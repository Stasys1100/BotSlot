
# bot.py
# Discord \u0431\u043e\u0442: \u0456\u043c\u043f\u043e\u0440\u0442 mission.sqm, \u0444\u0456\u043b\u044c\u0442\u0440\u0430\u0446\u0456\u044f \u0448\u0443\u043c\u0443, \u0432\u0438\u0431\u0456\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u044c \u043f\u043e \u0456\u043d\u0434\u0435\u043a\u0441\u0443,
# \u043e\u0431'\u0454\u0434\u043d\u0430\u043d\u043d\u044f \u0434\u0443\u0431\u043b\u0456\u043a\u0430\u0442\u0456\u0432, \u043f\u043e\u0432\u043d\u0438\u0439 \u0441\u043a\u043b\u0430\u0434 \u043c\u0456\u0436 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430\u043c\u0438, UI \u0434\u043b\u044f \u0441\u043b\u043e\u0442\u0456\u0432,
# \u0441\u0442\u0430\u0442\u0443\u0441/\u0434\u0435\u043f\u043b\u043e\u0439/\u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f, \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u043d\u044f \u0441\u043b\u043e\u0442\u0456\u0432.

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

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Logging \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("botslot")

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 ENV / INIT \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
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

DEFAULT_TITLE = "\u0412\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f"

# Debounce \u0434\u043b\u044f \u0456\u043c\u043f\u043e\u0440\u0442\u0443
_recent_imports: Dict[str, float] = {}
_RECENT_IMPORTS_TTL = 60.0  # \u0441\u0435\u043a

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Slot detection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
SLOT_KEYWORDS = [
    r'\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440', r'\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d', r'\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0441\u0442\u043e\u0440\u043e\u043d\u0438', r'\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0435\u043a\u0456\u043f\u0430\u0436\u0443',
    r'\u043f\u0456\u043b\u043e\u0442', r'\u043e\u043f\u0435\u0440\u0430\u0442\u043e\u0440', r'\u043d\u0430\u0432\u043e\u0434\u0447\u0438\u043a', r'\u0441\u0430\u043d\u0456\u0442\u0430\u0440', r'\u043c\u0435\u0434\u0438\u043a',
    r'\u0433\u0440\u0435\u043d\u0430\u0434\u0435\u0440', r'\u0433\u0440\u0430\u043d\u0430\u0442\u043e\u043c\u0435\u0442\u043d\u0438\u043a', r'\u043a\u0443\u043b\u0435\u043c\u0435\u0442\u043d\u0438\u043a', r'\u0441\u0442\u0440\u0456\u043b\u0435\u0446\u044c',
    r'\u0441\u0442\u0430\u0440\u0448\u0438\u0439 \u0441\u0442\u0440\u0456\u043b\u0435\u0446\u044c', r'\u0441\u043d\u0430\u0439\u043f\u0435\u0440', r'\u043a\u043e\u0440\u0438\u0433\u0443\u0432\u0430\u043b\u044c\u043d\u0438\u043a', r'\u043c\u0435\u0445\u0430\u043d\u0456\u043a-\u0432\u043e\u0434'
]
SLOT_RE = re.compile(r'^\s*(?:\d+\.\s*)?(' + r'|'.join(SLOT_KEYWORDS) + r')', flags=re.IGNORECASE)

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
def is_noise(s: str) -> bool:
    """
    \u0412\u0456\u0434\u0441\u0456\u043a\u0430\u0454 \u0441\u043b\u0443\u0436\u0431\u043e\u0432\u0438\u0439 \u0448\u0443\u043c: \u043b\u0456\u0442\u0435\u0440\u0430\u043b\u0438, \u043a\u0430\u043f\u0441/\u0456\u0434\u0435\u043d\u0442\u0438\u0444\u0456\u043a\u0430\u0442\u043e\u0440\u0438, \u043c\u043e\u0434\u0438/\u043f\u0440\u0430\u043f\u043e\u0440\u0438/\u0430\u0442\u0440\u0438\u0431\u0443\u0442\u0438, \u043c\u043e\u0434\u0435\u043b\u0456,
    one-off \u0442\u0435\u0445\u043d\u0456\u043a\u0430 \u0431\u0435\u0437 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\u0443, \u0441\u043b\u0443\u0436\u0431\u043e\u0432\u0456 \u043d\u0430\u0437\u0432\u0438.
    """
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()

    noise_literals = {
        "none","null","true","false",
        "army","default","platoon","standard","nochange",
        "uk","ukr","honor",
        "capture_1","defaultred","standardred",
        "everyone",
        "\u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f","\u0432\u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f",
        # \u0447\u0430\u0441\u0442\u043e \u0437\u0443\u0441\u0442\u0440\u0456\u0447\u0430\u044e\u0442\u044c\u0441\u044f \u0441\u043b\u0443\u0436\u0431\u043e\u0432\u0456 \u0431\u043b\u043e\u043a\u0438 \u043c\u0456\u0441\u0456\u0457
        "\u0437\u0441 \u0440\u0444 \u0442\u0430 \u043f\u0432\u043a"
    }
    if low in noise_literals:
        return True

    # \u0447\u0438\u0441\u0442\u0456 \u0447\u0438\u0441\u043b\u0430 \u0430\u0431\u043e \u0441\u0443\u0446\u0456\u043b\u044c\u043d\u0438\u0439 \u043a\u0430\u043f\u0441/\u0456\u0434\u0435\u043d\u0442\u0438\u0444\u0456\u043a\u0430\u0442\u043e\u0440
    if re.fullmatch(r'\d+', s):
        return True
    if re.fullmatch(r'[A-Z0-9_]+', s):
        return True

    # \u043c\u043e\u0434\u0438/\u043f\u0440\u0430\u043f\u043e\u0440\u0438/\u0441\u0435\u0440\u0432\u0456\u0441\u043d\u0456
    if ("rhs_" in low) or ("_hide" in low) or ("flag_manager" in low) or ("beacons" in low):
        return True
    if low.startswith("door_") or low.startswith("hatch") or "snorkel" in low or "plate" in low or "trunk" in low:
        return True
    
    # \u0434\u043e\u0434\u0430\u0442\u043a\u043e\u0432\u0430 \u0444\u0456\u043b\u044c\u0442\u0440\u0430\u0446\u0456\u044f \u0441\u043b\u0443\u0436\u0431\u043e\u0432\u0438\u0445 \u0442\u043e\u043a\u0435\u043d\u0456\u0432
    if re.search(r'\[\[\[\[.*?\]\]', s):  # [[[[],[]]...] \u0442\u043e\u043a\u0435\u043d\u0438
        return True
    if low.startswith("hide_") or low.startswith("rhs_") or low.startswith("door_"):
        return True

    # \u043e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432\u0430 \u0442\u0435\u0445\u043d\u0456\u043a\u0430 \u0431\u0435\u0437 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\u0443 (\u0440\u044f\u0434\u043e\u043a-\u0456\u0434\u0435\u043d\u0442\u0438\u0444\u0456\u043a\u0430\u0442\u043e\u0440)
    if low in {
        "mavicblue1","mavicblue2","mavicred1","mavicred2",
        "m113","m113a3","bmp","bmp-2","\u0431\u043c\u043f-2","\u043c\u0442-\u043b\u0431","gaz-66","\u0433\u0430\u0437-66","tigr","\u0442\u0438\u0433\u0440"
    }:
        return True

    # \u043c\u043e\u0434\u0435\u043b\u0456/\u043f\u0435\u0440\u0441\u043e\u043d\u0430\u0436\u0456
    if s.startswith("Male") and ("ENG" in s or "PER" in s or "RUS" in s):
        return True

    return False

def strip_quotes_semicolons(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'^['"]+|['"]+$', '', s.strip())
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
        return re.sub(r'\s{2,}', ' ', combined).strip(' "'')
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    return re.sub(r'\s{2,}', ' ', s).strip(' "'')

def looks_like_code_block(s: str) -> bool:
    if not s:
        return True
    if re.search(r'\b(condition|expression|init|compile|preprocessfilelinenumbers|thislist|playerSide|vehicle player)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'\\|\\|\\	', s):
        return True
    if re.search(r'[{}()\[\];=<>!|&\\]', s) and len(re.findall(r'[A-Za-z\u0410-\u042f\u0430-\u044f\u0401\u0451\u0407\u0457\u0406\u0456\u0404\u0454\u0490\u0491]', s)) < 5:
        return True
    return False

def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'^\s*\d+\.\s*', '', s)
    s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
    return s.strip(' "'')

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    return s.strip(" \	\
\-\u2013\u2014")

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

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Parser \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
TITLE_PATTERN = re.compile(
    r'@\u0410\u043b\u044c\u0444\u0430|\u0428\u0442\u0430\u0431|\u0431\u0440\u0438\u0433\u0430\u0434\u0430|\u041e\u043a\u0440\u0435\u043c\u0430|\u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f|\u041f\u0456\u0445\u043e\u0442\u043d\u0435|\u041e\u041c\u0411\u0440|\u0421\u0421\u041e|\u0413\u0423\u0420|\u0430\u0440\u0442\u0438\u043b\u0435\u0440\u0456\u044f|\u0427\u0412\u041a|\u0430\u0440\u043c\u0435\u0439\u0441\u044c\u043a\u0438\u0439|\u043c\u043e\u0442\u043e\u0441\u0442\u0440\u0456\u043b\u043a\u043e\u0432',
    flags=re.IGNORECASE
)

def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    \u0412\u0438\u0442\u044f\u0433\u0443\u0454 (title, [slots]) \u0437 description/value \u0442\u0430 <t>...</t>, \u0444\u0456\u043b\u044c\u0442\u0440\u0443\u0454 \u0448\u0443\u043c.
    - \u0417\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a: \u043d\u0430\u044f\u0432\u043d\u0456\u0441\u0442\u044c '|' \u0430\u0431\u043e TITLE_PATTERN.
    - \u0421\u043b\u043e\u0442\u0438: \u043d\u0443\u043c\u0435\u0440\u0430\u0446\u0456\u044f \u0430\u0431\u043e \u043a\u043b\u044e\u0447\u043e\u0432\u0456 \u0441\u043b\u043e\u0432\u0430 + \u0412\u0421\u0406 \u0456\u043d\u0448\u0456 \u0440\u044f\u0434\u043a\u0438 \u043c\u0456\u0436 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430\u043c\u0438, \u044f\u043a\u0449\u043e \u043d\u0435 \u0448\u0443\u043c \u0456 \u043d\u0435 \u043a\u043e\u0434.
    - \u0414\u0443\u0431\u043b\u0456\u043a\u0430\u0442 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430 \u2192 \u043e\u0431'\u0454\u0434\u043d\u0430\u043d\u043d\u044f \u0441\u043b\u043e\u0442\u0456\u0432, \u0437\u0431\u0435\u0440\u0456\u0433\u0430\u044e\u0447\u0438 \u043f\u043e\u0440\u044f\u0434\u043e\u043a \u0442\u0430 \u0443\u043d\u0456\u043a\u0430\u043b\u044c\u043d\u0456\u0441\u0442\u044c.
    """
    text = text.replace('\
', '\
').replace('', '\
')

    # \u0437\u0456\u0431\u0440\u0430\u0442\u0438 \u0432\u0441\u0456 candidate-\u0442\u0435\u043a\u0441\u0442\u0438
    candidates = [html.unescape(m.group(1)).strip()
                  for m in re.finditer(r'(?:description|value)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE)]
    candidates += [html.unescape(m).strip()
                   for m in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)]

    if not candidates:
        return []

    groups: Dict[str, List[str]] = {}  # title_norm -> slots

    cur_title: Optional[str] = None
    cur_slots: List[str] = []
    commander_in_title = False  # \u0444\u043b\u0430\u0433 \u0434\u043b\u044f \u0432\u0456\u0434\u0441\u0442\u0435\u0436\u0435\u043d\u043d\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 \u0432 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0443

    def flush():
        nonlocal cur_title, cur_slots, commander_in_title
        if cur_title is None and cur_slots:
            # \u044f\u043a\u0449\u043e \u0441\u043b\u043e\u0442\u0456\u0432 \u043d\u0430\u0431\u0440\u0430\u043b\u043e\u0441\u044f \u0431\u0435\u0437 \u044f\u0432\u043d\u043e\u0433\u043e \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430 \u2014 \u0432\u0438\u043a\u043e\u0440\u0438\u0441\u0442\u043e\u0432\u0443\u0454\u043c\u043e DEFAULT_TITLE
            title_line = DEFAULT_TITLE
        else:
            title_line = cur_title or DEFAULT_TITLE
            
        if cur_slots:
            # \u0444\u0456\u043b\u044c\u0442\u0440\u0430\u0446\u0456\u044f \u0442\u0430 \u043d\u043e\u0440\u043c\u0430\u043b\u0456\u0437\u0430\u0446\u0456\u044f
            slots = [normalize_slot_name(s) for s in cur_slots if s and not looks_like_code_block(s) and not is_noise(s)]
            slots = dedupe_preserve_order(slots)
            
            # \u0412\u0418\u041f\u0420\u0410\u0412\u041b\u0415\u041d\u041e: \u044f\u043a\u0449\u043e \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0431\u0443\u0432 \u0443 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0443, \u0434\u043e\u0434\u0430\u0454\u043c\u043e \u0439\u043e\u0433\u043e \u043f\u0435\u0440\u0448\u0438\u043c \u0441\u043b\u043e\u0442\u043e\u043c
            if commander_in_title and "\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f" in title_line:
                # \u0412\u0438\u0434\u0430\u043b\u044f\u0454\u043c\u043e "\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f" \u0437 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430
                title_line = title_line.replace("\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f", "").strip()
                # \u0414\u043e\u0434\u0430\u0454\u043c\u043e \u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u0430 \u043f\u0435\u0440\u0448\u0438\u043c \u0441\u043b\u043e\u0442\u043e\u043c
                slots.insert(0, "\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f")
                commander_in_title = False
            
            if slots:
                t_norm = re.sub(r'\s{2,}', ' ', title_line).strip()
                prev = groups.get(t_norm, [])
                # \u043e\u0431'\u0454\u0434\u043d\u0430\u0442\u0438, \u0437\u0431\u0435\u0440\u0456\u0433\u0430\u044e\u0447\u0438 \u043f\u043e\u0440\u044f\u0434\u043e\u043a \u0442\u0430 \u0443\u043d\u0456\u043a\u0430\u043b\u044c\u043d\u0456\u0441\u0442\u044c
                merged = prev + [x for x in slots if x not in prev]
                groups[t_norm] = merged
        cur_title, cur_slots = None, []
        commander_in_title = False

    for raw in candidates:
        s = re.sub(r'\s{2,}', ' ', raw).strip()
        if not s or is_noise(s):
            continue

        # \u043d\u043e\u0432\u0438\u0439 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a?
        if '|' in s or TITLE_PATTERN.search(s):
            flush()
            # \u0412\u0418\u041f\u0420\u0410\u0412\u041b\u0415\u041d\u041e: \u043f\u0435\u0440\u0435\u0432\u0456\u0440\u044f\u0454\u043c\u043e, \u0447\u0438 \u043c\u0456\u0441\u0442\u0438\u0442\u044c \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a "\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f"
            if "\u041a\u043e\u043c\u0430\u043d\u0434\u0438\u0440 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f" in s:
                commander_in_title = True
            cur_title = s
            continue

        # \u0441\u043b\u043e\u0442 \u0437\u0430 \u043d\u0443\u043c\u0435\u0440\u0430\u0446\u0456\u0454\u044e / \u043a\u043b\u044e\u0447\u043e\u0432\u0438\u043c\u0438 \u0441\u043b\u043e\u0432\u0430\u043c\u0438
        if re.match(r'^\s*\d+\.\s*', s) or SLOT_RE.search(s):
            slot = clean_line_for_slot(s)
            if slot and not looks_like_code_block(slot) and not is_noise(slot):
                cur_slots.append(slot)
            continue

        # \u0431\u0443\u0434\u044c-\u044f\u043a\u0438\u0439 \u0456\u043d\u0448\u0438\u0439 \u0442\u0435\u043a\u0441\u0442 \u043c\u0456\u0436 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430\u043c\u0438 \u2192 \u0441\u043b\u043e\u0442, \u044f\u043a\u0449\u043e \u043d\u0435 \u0448\u0443\u043c \u0456 \u043d\u0435 \u043a\u043e\u0434
        if not looks_like_code_block(s) and not is_noise(s):
            cur_slots.append(clean_line_for_slot(s))

    flush()

    # \u043f\u0435\u0440\u0435\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u0432 \u0441\u043f\u0438\u0441\u043e\u043a
    return [(title, slots) for title, slots in groups.items()]

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 \u0421\u0430\u0439\u0434-\u0434\u0435\u0442\u0435\u043a\u0442\u043e\u0440 (\u043e\u043f\u0446. \u0434\u043b\u044f \u0441\u043e\u0440\u0442\u0443\u0432\u0430\u043d\u043d\u044f) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
def detect_side_from_title(title: str) -> str:
    t = title.lower()
    # \u0441\u043f\u0440\u043e\u0449\u0435\u043d\u043e: \u0443\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430 \u0441\u0442\u043e\u0440\u043e\u043d\u0430
    if any(k in t for k in ["\u043e\u043c\u0431\u0440", "\u0437\u0441\u0443", "\u0433\u0443\u043f", "\u0441\u0441\u043e", "\u043e\u043a\u0440\u0435\u043c\u0430", "\u0431\u0440\u0438\u0433\u0430\u0434\u0430", "\u0430\u043b\u044c\u0444\u0430"]):
        return "\u0417\u0421\u0423"
    # \u0440\u043e\u0441 \u0441\u0442\u043e\u0440\u043e\u043d\u0430 / \u041f\u0412\u041a
    if any(k in t for k in ["\u0430\u0440\u043c\u0435\u0439\u0441\u044c\u043a\u0438\u0439", "\u0447\u0432\u043a", "\u043c\u043e\u0442\u043e\u0441\u0442\u0440\u0456\u043b\u043a\u043e\u0432\u0430", "\u043a\u043e\u0440\u043f\u0443\u0441", "\u043e\u0442\u0434\u0435\u043b\u044c\u043d\u0430\u044f", "\u0430\u0440\u043c\u0438\u044f"]):
        return "\u0417\u0421 \u0420\u0424/\u041f\u0412\u041a"
    return "\u041d\u0435\u0432\u0456\u0434\u043e\u043c\u043e"

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 UI helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
def build_embed(sess: dict) -> discord.Embed:
    embed = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    owners = sess.get("owners", [None] * len(sess["lines"]))
    for i, (text, owner) in enumerate(zip(sess["lines"], owners)):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} \u2013 \u0417\u0430\u0439\u043d\u044f\u0442\u043e {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    embed.description = "\
".join(lines)
    return embed

class SlotButton(Button):
    def __init__(self, sid: int, idx: int):
        owner = sessions[sid]["owners"][idx]
        free = owner is None
        label = f"{idx+1}. {'\u0417\u0430\u0439\u043d\u044f\u0442\u0438' if free else '\u0412\u0456\u0434\u043c\u043e\u0432\u0438\u0442\u0438\u0441\u044f'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        # \u0417\u0430\u0431\u043e\u0440\u043e\u043d\u0430 \u043c\u043d\u043e\u0436\u0438\u043d\u043d\u0438\u0445 \u0441\u043b\u043e\u0442\u0456\u0432 \u0443 \u0442\u0456\u0439 \u0436\u0435 \u0433\u0456\u043b\u044c\u0446\u0456
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == sess["channel_id"] and user in s.get("owners", []):
                    return await inter.response.send_message("\u26a0\ufe0f \u0412\u0438 \u0432\u0436\u0435 \u043c\u0430\u0454\u0442\u0435 \u0441\u043b\u043e\u0442 \u0432 \u0446\u0456\u0439 \u0433\u0456\u043b\u044c\u0446\u0456.", ephemeral=True)
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))
        return await inter.response.send_message(f"\u26a0\ufe0f \u0426\u0435\u0439 \u0441\u043b\u043e\u0442 \u0437\u0430\u0439\u043d\u044f\u0442\u043e {owner.mention}.", ephemeral=True)

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Claim flow: \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u043d\u044f \u0441\u043b\u043e\u0442\u0456\u0432 \u0447\u0435\u0440\u0435\u0437 \u043c\u043e\u0434\u0430\u043b \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
class RemoveSlotModal(Modal):
    def __init__(self, sid: int, idx: int):
        super().__init__(title="\u041f\u0440\u0438\u0447\u0438\u043d\u0430 \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u043d\u044f")
        self.sid, self.idx = sid, idx
        self.reason = TextInput(label="\u041f\u0440\u0438\u0447\u0438\u043d\u0430", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        reason = self.reason.value
        if not owner:
            return await inter.response.send_message(f"\u26a0\ufe0f \u0421\u043b\u043e\u0442 #{self.idx+1} \u0432\u0436\u0435 \u0432\u0456\u043b\u044c\u043d\u0438\u0439.", ephemeral=True)
        sess["owners"][self.idx] = None
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass
        try:
            await owner.send(f"\u203c\ufe0f \u0412\u0438 \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u0456 \u0437\u0456 \u0441\u043b\u043e\u0442\u0443 #{self.idx+1} \u0443 \u00ab{sess['title']}\u00bb.\
\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}")
        except: pass
        await inter.response.send_message(f"\u2705 \u0421\u043b\u043e\u0442 #{self.idx+1} \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u043e.", ephemeral=True)

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

@bot.command(name="\u0437\u0432\u0456\u043b\u044c\u043d\u0438\u0442\u0438")
async def \u0437\u0432\u0456\u043b\u044c\u043d\u0438\u0442\u0438(ctx: commands.Context, session_msg_id: int):
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("\u274c \u0426\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u043b\u0438\u0448\u0435 \u0432 \u0430\u0434\u043c\u0456\u043d\u0456\u0441\u0442\u0440\u0430\u0442\u0438\u0432\u043d\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0456.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"\u274c \u0421\u0435\u0441\u0456\u044f \u0437 ID {session_msg_id} \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u0430.")
    await ctx.send(f"\ud83d\udccb \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0441\u043b\u043e\u0442 \u0434\u043b\u044f \u0437\u0432\u0456\u043b\u044c\u043d\u0435\u043d\u043d\u044f \u0432 \u0441\u0435\u0441\u0456\u0457 {session_msg_id}:", view=RemoveSlotView(session_msg_id))

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Commands \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
@bot.command(name="\u0456\u043c\u043f\u043e\u0440\u0442_sqm", aliases=["import_sqm"])
async def \u0456\u043c\u043f\u043e\u0440\u0442_sqm(ctx: commands.Context, *filter_ids: str):
    """
    \u0406\u043c\u043f\u043e\u0440\u0442 \u0442\u0435\u043a\u0441\u0442\u043e\u0432\u043e\u0433\u043e mission.sqm.
    - \u0411\u0435\u0437 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u0456\u0432: \u0432\u0438\u0432\u043e\u0434\u0438\u0442\u044c \u0443\u0441\u0456 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u043d\u044f \u043f\u043e \u0433\u0440\u0443\u043f\u0430\u0445 (\u0434\u0443\u0431\u043b\u0456\u043a\u0430\u0442\u0438 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0456\u0432 \u043e\u0431'\u0454\u0434\u043d\u0430\u043d\u0456).
    - \u0417 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u0430\u043c\u0438 (\u043d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434 "2-2" \u0430\u0431\u043e "1-2 2-5"): \u043f\u043e\u043a\u0430\u0437\u0443\u0454 \u043b\u0438\u0448\u0435 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u043d\u0456 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0438 (\u044f\u043a \u043e\u043a\u0440\u0435\u043c\u0438\u0439 \u0442\u043e\u043a\u0435\u043d), \u0434\u043b\u044f \u0432\u0441\u0456\u0445 \u0441\u0442\u043e\u0440\u0456\u043d.
    """
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u043b\u0438\u0448\u0435 \u0432 \u0430\u0434\u043c\u0456\u043d\u0456\u0441\u0442\u0440\u0430\u0442\u0438\u0432\u043d\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0456.")
    if not ctx.message.attachments:
        return await ctx.send("\u274c \u041f\u0440\u0438\u043a\u0440\u0456\u043f\u0456\u0442\u044c mission.sqm \u0430\u0431\u043e mission.txt")

    att = ctx.message.attachments[0]
    key = f"{ctx.message.id}:{att.id}"
    now = time.time()
    # cleanup \u0441\u0442\u0430\u0440\u0438\u0445 \u0437\u0430\u043f\u0438\u0441\u0456\u0432
    for k, t in list(_recent_imports.items()):
        if now - t > _RECENT_IMPORTS_TTL:
            _recent_imports.pop(k, None)
    if key in _recent_imports:
        return await ctx.send("\u26a0\ufe0f \u0426\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0432\u0436\u0435 \u043e\u0431\u0440\u043e\u0431\u043b\u044f\u0454\u0442\u044c\u0441\u044f (\u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0435 \u043d\u0430\u0434\u0445\u043e\u0434\u0436\u0435\u043d\u043d\u044f).")
    _recent_imports[key] = now

    try:
        raw = await att.read()
        text = decode_bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as e:
        _recent_imports.pop(key, None)
        logger.exception("Failed to read attachment")
        return await ctx.send(f"\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u0438 \u0432\u043a\u043b\u0430\u0434\u0435\u043d\u043d\u044f: {e}")

    # \u041f\u0430\u0440\u0441\u0438\u043d\u0433
    try:
        groups = extract_units_and_slots(text)
    except Exception:
        logger.exception("Parser crashed")
        groups = []

    # \u0412\u0418\u041f\u0420\u0410\u0412\u041b\u0415\u041d\u041e: \u0441\u043f\u0440\u043e\u0449\u0435\u043d\u0430 \u0444\u0456\u043b\u044c\u0442\u0440\u0430\u0446\u0456\u044f \u043f\u043e \u0456\u043d\u0434\u0435\u043a\u0441\u0430\u0445
    if filter_ids:
        patterns = [re.compile(rf'\b{re.escape(fid)}\b') for fid in filter_ids]
        filtered = []
        for title, slots in groups:
            if any(p.search(title or "") for p in patterns):
                filtered.append((title, slots))
        groups = filtered

    # \u0412\u0418\u041f\u0420\u0410\u0412\u041b\u0415\u041d\u041e: \u044f\u043a\u0449\u043e \u0454 \u0433\u0440\u0443\u043f\u0438 \u043f\u0456\u0441\u043b\u044f \u0444\u0456\u043b\u044c\u0442\u0440\u0430\u0446\u0456\u0457, \u0432\u0438\u0432\u043e\u0434\u0438\u043c\u043e \u0457\u0445 \u0431\u0435\u0437 \u0437\u0430\u0439\u0432\u0438\u0445 \u043f\u043e\u043f\u0435\u0440\u0435\u0434\u0436\u0435\u043d\u044c
    if not groups:
        _recent_imports.pop(key, None)
        if filter_ids:
            await ctx.send(f"\u26a0\ufe0f \u041d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u044c \u0437 \u0456\u043d\u0434\u0435\u043a\u0441\u0430\u043c\u0438: {', '.join(filter_ids)}.")
        else:
            await ctx.send("\u26a0\ufe0f \u041d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u044c \u0430\u0431\u043e \u0441\u043b\u043e\u0442\u0456\u0432 \u0443 \u0446\u044c\u043e\u043c\u0443 \u0444\u0430\u0439\u043b\u0456.")
        return

    # \u0412\u0456\u0434\u043f\u0440\u0430\u0432\u043a\u0430: \u0433\u0440\u0443\u043f\u0443\u0454\u043c\u043e \u043f\u043e \u0441\u0442\u043e\u0440\u043e\u043d\u0456 (\u0417\u0421\u0423 / \u0417\u0421 \u0420\u0424/\u041f\u0412\u041a / \u041d\u0435\u0432\u0456\u0434\u043e\u043c\u043e) \u0434\u043b\u044f \u0447\u0438\u0442\u0430\u0431\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u0456
    by_side: Dict[str, List[Tuple[str, List[str]]]] = {"\u0417\u0421\u0423": [], "\u0417\u0421 \u0420\u0424/\u041f\u0412\u041a": [], "\u041d\u0435\u0432\u0456\u0434\u043e\u043c\u043e": []}
    for title, slots in groups:
        side = detect_side_from_title(title)
        by_side[side].append((title, slots))

    sent = 0
    for side in ("\u0417\u0421\u0423", "\u0417\u0421 \u0420\u0424/\u041f\u0412\u041a", "\u041d\u0435\u0432\u0456\u0434\u043e\u043c\u043e"):
        blocks = by_side[side]
        if not blocks:
            continue
        # \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a \u0441\u0435\u043a\u0446\u0456\u0457 \u0434\u043b\u044f \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\u0443 (\u043d\u0435 \u0448\u0443\u043c)
        await ctx.send(f"```{side}```")
        for title, slots in blocks:
            out = "\
".join([title] + slots)
            # \u0447\u0430\u043d\u043a, \u044f\u043a\u0449\u043e \u0434\u0443\u0436\u0435 \u0434\u043e\u0432\u0433\u043e
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
    await ctx.send(f"\u2705 \u0413\u043e\u0442\u043e\u0432\u043e. \u041e\u043f\u0443\u0431\u043b\u0456\u043a\u043e\u0432\u0430\u043d\u043e \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u044c: {sent}.")

@bot.command(name="\u0441\u0442\u043e\u043f", aliases=["stop"])
async def \u0441\u0442\u043e\u043f(ctx: commands.Context):
    global _stop_sending_global, _stop_sending_by_channel
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("\u274c \u0426\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u043b\u0438\u0448\u0435 \u0432 \u0430\u0434\u043c\u0456\u043d\u0456\u0441\u0442\u0440\u0430\u0442\u0438\u0432\u043d\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0456.")
    _stop_sending_global = True
    _stop_sending_by_channel[ctx.channel.id] = True
    await ctx.send("\u23f9\ufe0f \u0417\u0443\u043f\u0438\u043d\u044f\u044e \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043a\u0443 \u0432\u0456\u0434\u0434\u0456\u043b\u0435\u043d\u044c...")

@bot.command(name="\u043e\u043d\u043e\u0432\u0438\u0442\u0438", aliases=["update"])
async def _\u043e\u043d\u043e\u0432\u0438\u0442\u0438(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("\u274c DEPLOY_HOOK_URL \u043d\u0435 \u0432\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("\ud83d\udd04 \u0414\u0435\u043f\u043b\u043e\u0439 \u0442\u0440\u0438\u0433\u0435\u0440\u043e\u0432\u0430\u043d\u043e!")

@bot.command(name="\u0441\u0442\u0430\u0442\u0443\u0441", aliases=["status"])
async def _\u0441\u0442\u0430\u0442\u0443\u0441(ctx: commands.Context):
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    await ctx.send(f"\ud83e\udde0 Commit: `{commit}`\
\ud83d\udcca Sessions: {len(sessions)}\
\ud83d\udccb Claims: {sum(len(v) for v in claims.values())}")

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Reminder (optional) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        if VTG_CHANNEL_ID:
            ch = bot.get_channel(VTG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send("||@everyone||\
**\u0417\u0431\u0456\u0440 VTG**")
                except Exception:
                    logger.exception("vtg_reminder send failed")

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 on_ready / on_message \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
@bot.event
async def on_ready():
    logger.info("Bot ready: %s", bot.user)
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    embed = discord.Embed(title="\ud83d\udd04 \u0411\u043e\u0442 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0449\u0435\u043d\u043e", description=f"\ud83d\udce6 Commit: `{commit}`", color=discord.Color.green())
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
    if "\u0437\u0430\u043f\u0438\u0441 \u0441\u043b\u043e\u0442" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "\u0437\u0430\u043f\u0438\u0441 \u0441\u043b\u043e\u0442" in txt.lower() or "everyone" in txt.lower():
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

# \u2500\u2500\u2500\u2500\u2500\u2500\u2500 Run \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
if not TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit(1)
bot.run(TOKEN)
