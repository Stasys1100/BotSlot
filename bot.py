# bot.py
# Discord бот: локальний витяг mission.sqm з .pbo через ExtractPbo (Mikero),
# очищення тексту, парсинг відділень і публікація слотів.
# .env: DISCORD_TOKEN, ADMIN_CHANNEL_ID, VTG_CHANNEL_ID (опц.), EXTRACTPBO_PATH

import os
import re
import io
import zipfile
import html
import json
import subprocess
import datetime
import tempfile
import shutil
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict, Any

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# keep_alive optional
try:
    from keep_alive import keep_alive
    keep_alive()
except Exception:
    pass

# optional encoding detectors
try:
    from charset_normalizer import from_bytes as cn_from_bytes
except Exception:
    cn_from_bytes = None
try:
    import chardet
except Exception:
    chardet = None

# ─── ENV / INIT ───────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 1160843618433630228)
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 1395065909185478769)

# ExtractPbo path from .env (full path to ExtractPbo.exe)
EXTRACTPBO_PATH = os.getenv("EXTRACTPBO_PATH", "ExtractPbo")

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}
claims: dict[tuple[int,int], list] = {}
request_counter = 0

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── Reminder ─────────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except:
                pass

# ─── Utilities: encoding, mojibake fixes ──────────────────────────────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def detect_encoding_bytes(b: bytes) -> Optional[str]:
    if cn_from_bytes:
        try:
            results = cn_from_bytes(b)
            if results:
                best = results.best()
                if best and best.encoding:
                    return best.encoding.lower()
        except Exception:
            pass
    if chardet:
        try:
            res = chardet.detect(b)
            enc = res.get('encoding')
            if enc:
                return enc.lower()
        except Exception:
            pass
    return None

def _cyrillic_score(s: str) -> int:
    return len(re.findall(r'[\u0400-\u04FF]', s))

def _try_redecode_candidates(original_bytes: bytes, initial_text: str) -> Tuple[str, str]:
    candidates: List[Tuple[str, str]] = []
    if initial_text is not None:
        candidates.append((initial_text, "as-decoded"))
    tried_encs = ["utf-8", "cp1251", "windows-1251", "koi8-r", "latin-1"]
    for enc in tried_encs:
        try:
            txt = original_bytes.decode(enc, errors="replace")
            candidates.append((txt, enc))
        except Exception:
            pass
    try:
        redecoded = initial_text.encode("latin-1", errors="replace").decode("utf-8", errors="replace")
        candidates.append((redecoded, "latin1->utf8"))
    except Exception:
        pass
    try:
        redecoded = initial_text.encode("latin-1", errors="replace").decode("cp1251", errors="replace")
        candidates.append((redecoded, "latin1->cp1251"))
    except Exception:
        pass
    try:
        redecoded = initial_text.encode("cp1251", errors="replace").decode("utf-8", errors="replace")
        candidates.append((redecoded, "cp1251->utf8"))
    except Exception:
        pass
    def score(item: Tuple[str, str]) -> Tuple[int, float]:
        txt, _ = item
        cyr = _cyrillic_score(txt)
        printable = sum(1 for ch in txt if ch.isprintable()) / max(1, len(txt))
        return (cyr, printable)
    best = max(candidates, key=score)
    return best[0], best[1]

def fix_mojibake_text(text: str) -> str:
    if not text:
        return text
    if not re.search(r'[ÐР]', text):
        return text
    candidates: List[Tuple[str, str]] = []
    candidates.append((text, "orig"))
    try:
        candidates.append((text.encode("latin-1", errors="replace").decode("utf-8", errors="replace"), "latin1->utf8"))
    except Exception:
        pass
    try:
        candidates.append((text.encode("cp1251", errors="replace").decode("utf-8", errors="replace"), "cp1251->utf8"))
    except Exception:
        pass
    try:
        candidates.append((text.encode("utf-8", errors="replace").decode("cp1251", errors="replace"), "utf8->cp1251"))
    except Exception:
        pass
    try:
        candidates.append((text.encode("latin-1", errors="replace").decode("cp1251", errors="replace"), "latin1->cp1251"))
    except Exception:
        pass
    try:
        b = text.encode("utf-8", errors="replace")
        candidates.append((b.decode("cp1251", errors="replace"), "utf8bytes->cp1251"))
    except Exception:
        pass
    def score_item(it: Tuple[str, str]) -> Tuple[int, float]:
        t = it[0]
        return (_cyrillic_score(t), sum(1 for ch in t if ch.isprintable()) / max(1, len(t)))
    best = max(candidates, key=score_item)
    return best[0]

# ─── HTML / <t> extraction and cleaning ──────────────────────────────────────
def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if chunks:
        inner = " ".join(chunks)
        inner = re.sub(r'<[^>]+>', ' ', inner)
        inner = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', inner)
        inner = re.sub(r'\s{2,}', ' ', inner).strip(' "\'').strip()
        inner = fix_mojibake_text(inner)
        if re.search(r'Р\s?С', inner):
            try:
                inner = inner.encode('latin-1', errors='replace').decode('utf-8', errors='replace')
                inner = fix_mojibake_text(inner)
            except Exception:
                pass
        return inner
    s = re.sub(r'<\s*t\b[^>]*>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'</\s*t\s*>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    s = fix_mojibake_text(s)
    return s

def clean_slot_value(raw: str) -> str:
    if raw is None:
        return "Слот"
    s = extract_structured_text(raw)
    s = fix_mojibake_text(s)
    s = s.replace('\\', '/')
    s = re.sub(r'\.sqf\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\.pbo\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bhttps?://\S+\b', '', s)
    s = re.sub(r'\b[A-Za-z0-9_\\/:.\-]{40,}\b', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    if not s or s.lower().startswith("<t color") or s in {'"', "'"}:
        return "Слот"
    return s or "Слот"

# ─── RAP aggressive decoder ──────────────────────────────────────────────────
def rap_to_text_aggressive(data: bytes) -> str:
    detected = detect_encoding_bytes(data)
    initial_text = None
    tried_label = "unknown"
    if detected:
        try:
            initial_text = data.decode(detected, errors="replace")
            tried_label = detected
        except Exception:
            initial_text = None
    if initial_text is None:
        for enc in ("utf-8", "cp1251", "windows-1251", "koi8-r", "latin-1"):
            try:
                initial_text = data.decode(enc, errors="replace")
                tried_label = enc
                break
            except Exception:
                continue
    if initial_text is None:
        initial_text = data.decode("latin-1", errors="replace")
        tried_label = "latin-1-fallback"
    best_text, best_enc = _try_redecode_candidates(data, initial_text)
    if best_enc and best_enc != "as-decoded":
        tried_label = best_enc
    text = best_text
    text = fix_mojibake_text(text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+', ' ', text)
    def keep(ch):
        o = ord(ch)
        return (32 <= o <= 126) or (0x0400 <= o <= 0x04FF) or ch in '\n\r\t{}=;"\'.,:-_()[]/\\<>|'
    filtered = ''.join(ch if keep(ch) else ' ' for ch in text)
    keywords = ['class Group', 'class Unit', 'groupName', 'unitName', 'description', 'name', 'title']
    low = filtered.lower()
    indices = []
    for kw in keywords:
        start = 0
        k = kw.lower()
        while True:
            idx = low.find(k, start)
            if idx == -1:
                break
            indices.append(idx)
            start = idx + len(k)
    if indices:
        window = 5000
        frags = []
        for idx in sorted(set(indices)):
            s = max(0, idx - 400)
            e = min(len(filtered), idx + window)
            frags.append(filtered[s:e])
        candidate = '\n'.join(frags)
    else:
        candidate = filtered[:300000]
    candidate = candidate.replace('};', '};\n')
    candidate = re.sub(r';\s*', ';\n', candidate)
    candidate = re.sub(r'\{\s*', '{\n', candidate)
    candidate = re.sub(r'\s*\}\s*', '\n}\n', candidate)
    lines = [l.strip() for l in candidate.splitlines() if l.strip()]
    candidate = '\n'.join(lines)
    candidate = _normalize_whitespace(candidate)
    candidate = candidate.replace("'", '"')
    return f"// rap_decoded (encoding={tried_label})\n{candidate}"

def is_likely_rap_or_binary(b: bytes) -> bool:
    if len(b) >= 3 and b[:3] == b'raP':
        return True
    printable = sum(1 for ch in b if 32 <= ch <= 126 or ch in (9,10,13))
    ratio = printable / max(1, len(b))
    return ratio < 0.6

# ─── PBO extraction helpers ──────────────────────────────────────────────────
def extract_mission_from_pbo_bytes_zip(pbo_bytes: bytes) -> Optional[bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(pbo_bytes))
        for name in z.namelist():
            if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                return z.read(name)
    except Exception:
        pass
    return None

def extract_mission_with_extractpbo(pbo_bytes: bytes, timeout: int = 60) -> str:
    """
    Використовує ExtractPbo (EXTRACTPBO_PATH) для витягу mission.sqm з .pbo байтів.
    Повертає текст mission.sqm (str) або викидає помилку.
    """
    tmpdir = tempfile.mkdtemp(prefix="extractpbo_")
    try:
        pbo_path = os.path.join(tmpdir, "upload.pbo")
        with open(pbo_path, "wb") as f:
            f.write(pbo_bytes)
        outdir = os.path.join(tmpdir, "out")
        os.makedirs(outdir, exist_ok=True)

        # Команда: підлаштуй аргументи під версію ExtractPbo, якщо потрібно
        cmd = [EXTRACTPBO_PATH, "-o", outdir, pbo_path]

        # Виконання без shell
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)

        # Пошук mission.sqm
        for root, _, files in os.walk(outdir):
            for name in files:
                if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                    with open(os.path.join(root, name), "rb") as sf:
                        raw = sf.read()
                        # якщо файл виглядає бінарним — застосуємо агресивний декодер
                        if is_likely_rap_or_binary(raw):
                            return rap_to_text_aggressive(raw)
                        # спробуємо декодувати виявленим кодуванням або utf-8
                        enc = detect_encoding_bytes(raw) or "utf-8"
                        try:
                            return raw.decode(enc, errors="replace")
                        except Exception:
                            return raw.decode("utf-8", errors="replace")
        # фолбек: спробувати zip-витяг (якщо ExtractPbo не знайшов)
        zres = extract_mission_from_pbo_bytes_zip(pbo_bytes)
        if zres:
            if is_likely_rap_or_binary(zres):
                return rap_to_text_aggressive(zres)
            enc = detect_encoding_bytes(zres) or "utf-8"
            return zres.decode(enc, errors="replace")
        raise FileNotFoundError("mission.sqm not found after ExtractPbo extraction")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# ─── Parser for mission.sqm (flexible) ───────────────────────────────────────
def parse_mission_sqm_flexible(text: str) -> List[Tuple[str, List[str]]]:
    groups: List[Tuple[str, List[str]]] = []
    txt = text.replace('\r\n', '\n')
    group_blocks = re.findall(r'(class\s+Group\b.*?\{.*?\}[\s;]*)', txt, flags=re.IGNORECASE | re.DOTALL)
    if group_blocks:
        for blk in group_blocks:
            mname = re.search(r'(?:name|groupName|title)\s*=\s*"(.*?)"\s*;', blk, flags=re.IGNORECASE | re.DOTALL)
            if not mname:
                mname = re.search(r'(?:name|groupName|title)\s*=\s*[\'"](.*?)[\'"]', blk, flags=re.IGNORECASE | re.DOTALL)
            gname = clean_slot_value(mname.group(1)) if mname else "Відділення"
            units = re.findall(r'class\s+Unit\b.*?\{(.*?)\}', blk, flags=re.IGNORECASE | re.DOTALL)
            slots = []
            for u in units:
                mslot = re.search(r'(?:description|unitName|text)\s*=\s*"(.*?)"\s*;', u, flags=re.IGNORECASE | re.DOTALL)
                if not mslot:
                    mslot = re.search(r'(?:description|unitName|text)\s*=\s*[\'"](.*?)[\'"]', u, flags=re.IGNORECASE | re.DOTALL)
                if mslot:
                    slots.append(clean_slot_value(mslot.group(1)))
                    continue
                arr = re.findall(r'(?:description|unitName|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', u, flags=re.IGNORECASE | re.DOTALL)
                if arr:
                    items = re.findall(r'[\'"](.+?)[\'"]', arr[0], flags=re.DOTALL)
                    for it in items:
                        slots.append(clean_slot_value(it))
                    continue
                mslot2 = re.search(r'(?:name)\s*=\s*"(.*?)"\s*;', u, flags=re.IGNORECASE | re.DOTALL)
                if not mslot2:
                    mslot2 = re.search(r'(?:name)\s*=\s*[\'"](.*?)[\'"]', u, flags=re.IGNORECASE | re.DOTALL)
                if mslot2:
                    slots.append(clean_slot_value(mslot2.group(1)))
                    continue
                q = re.search(r'"([^"]{2,200})"', u, flags=re.DOTALL)
                slots.append(clean_slot_value(q.group(1)) if q else "Слот")
            groups.append((gname, slots))
        return groups
    for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"(.*?)"\s*;', txt, flags=re.IGNORECASE | re.DOTALL):
        gname = clean_slot_value(m.group(1))
        start = m.end()
        frag = txt[start:start + 30000]
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"\s*;', frag, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            units = re.findall(r'(?:unitName|description|text)\s*=\s*[\'"](.*?)[\'"]', frag, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', frag, flags=re.IGNORECASE | re.DOTALL)
            for a in arrs:
                units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
        if units:
            groups.append((gname, [clean_slot_value(u) for u in units]))
    if groups:
        return groups
    unit_matches = [(m.start(), clean_slot_value(m.group(1))) for m in re.finditer(r'(?:unitName|description|text)\s*=\s*"(.*?)"', txt, flags=re.IGNORECASE | re.DOTALL)]
    group_markers = [m.start() for m in re.finditer(r'(?:class\s+Group\b|groupName|name|title)\s*=', txt, flags=re.IGNORECASE)]
    if unit_matches:
        if not group_markers:
            return [("Відділення", [u for _, u in unit_matches])]
        grouped: Dict[int, List[str]] = {}
        for pos, uname in unit_matches:
            prev_positions = [p for p in group_markers if p <= pos]
            key = max(prev_positions) if prev_positions else -1
            grouped.setdefault(key, []).append(uname)
        for key, slots in grouped.items():
            if key == -1:
                gname = "Відділення"
            else:
                snippet = txt[key:key+400]
                mname2 = re.search(r'(?:name|groupName|title)\s*=\s*"(.*?)"', snippet, flags=re.IGNORECASE | re.DOTALL)
                if not mname2:
                    mname2 = re.search(r'(?:name|groupName|title)\s*=\s*[\'"](.*?)[\'"]', snippet, flags=re.IGNORECASE | re.DOTALL)
                gname = clean_slot_value(mname2.group(1)) if mname2 else "Відділення"
            groups.append((gname, slots))
        return groups
    return []

# ─── Command: import_sqm (uses ExtractPbo for .pbo) ──────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")
    attachment = ctx.message.attachments[0]
    await ctx.send("🔄 Обробка файлу...")

    sqm_text = None
    method = None

    try:
        if attachment.filename.lower().endswith(".pbo"):
            raw = await attachment.read()
            try:
                sqm_text = extract_mission_with_extractpbo(raw)
                method = "local:ExtractPbo"
            except subprocess.CalledProcessError as e:
                return await ctx.send(f"❌ ExtractPbo помилка: {e}")
            except FileNotFoundError:
                return await ctx.send("❌ Не знайдено mission.sqm у PBO після витягу.")
            except Exception as e:
                return await ctx.send(f"❌ Помилка при витягу PBO: {e}")
        else:
            # .sqm or other plain text
            data = await attachment.read()
            if is_likely_rap_or_binary(data):
                sqm_text = rap_to_text_aggressive(data)
                method = "local:rap"
            else:
                enc = detect_encoding_bytes(data) or "utf-8"
                sqm_text = data.decode(enc, errors="replace")
                method = enc
    except Exception as e:
        return await ctx.send(f"❌ Помилка читання вкладення: {e}")

    # парсимо отриманий текст
    if not sqm_text:
        return await ctx.send("⚠️ Не вдалося отримати текст з файлу.")

    groups = parse_mission_sqm_flexible(sqm_text)
    if not groups:
        # фолбек: знайти unitName/description/text
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"', sqm_text, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            units = re.findall(r"(?:unitName|description|text)\s*=\s*'(.*?)'", sqm_text, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', sqm_text, flags=re.IGNORECASE | re.DOTALL)
            for a in arrs:
                units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
        if units:
            groups = [("Відділення", [clean_slot_value(u) for u in units])]

    # діагностика (коротко)
    dbg_lines = []
    raw_t_inner = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', sqm_text, flags=re.IGNORECASE | re.DOTALL)[:5]
    if raw_t_inner:
        dbg_lines.append("first <t>inner = " + (raw_t_inner[0][:300] + '...' if len(raw_t_inner[0])>300 else raw_t_inner[0]))

    emb_dbg = discord.Embed(title="🔍 Діагностика імпорту mission.sqm", color=discord.Color.orange())
    emb_dbg.add_field(name="Метод обробки", value=method or "unknown", inline=False)
    if dbg_lines:
        emb_dbg.add_field(name="Приклади raw-захоплень", value="\n".join(dbg_lines)[:1000], inline=False)
    try:
        await ctx.send(embed=emb_dbg)
    except:
        pass

    if not groups:
        preview = (sqm_text or "")[:2000]
        emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.red())
        emb.add_field(name="Метод обробки", value=method or "unknown", inline=False)
        emb.add_field(name="Прев'ю початку файлу", value=f"```{preview}```", inline=False)
        try:
            buf = io.BytesIO((sqm_text or "").encode('utf-8'))
            buf.seek(0)
            file = discord.File(fp=buf, filename="decoded_mission_sqm.txt")
            await ctx.send(embed=emb, file=file)
        except Exception:
            await ctx.send(embed=emb)
        return

    # публікація відділень у адмін-каналі
    target_ch = bot.get_channel(ADMIN_CHANNEL_ID)
    if not target_ch:
        return await ctx.send("❌ Адмін-канал не знайдено за ID.")

    sent_count = 0
    total_slots = 0
    for name, slots in groups:
        cleaned = [clean_slot_value(s) for s in slots][:25]
        total_slots += len(cleaned)
        embed = discord.Embed(title=name or "Відділення", description="\n".join(f"{i+1}. {s}" for i,s in enumerate(cleaned)) or "— слотів не знайдено —", color=discord.Color.blurple())
        try:
            await target_ch.send(embed=embed)
            sent_count += 1
        except Exception:
            pass

    await ctx.send(f"✅ Імпорт завершено. Опубліковано відділень: {sent_count}. Метод: `{method}`. Знайдено слотів: {total_slots}.")

# ─── Slot UI and management (existing logic preserved) ───────────────────────
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
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
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
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
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

# ─── on_ready / on_message / сервісні команди ─────────────────────────────────
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
