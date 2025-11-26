# bot.py
import os
import re
import io
import zipfile
import html
import subprocess
import datetime
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# optional keep-alive (if you have such a module)
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
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 1160843618433630228)
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 1395065909185478769)

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}              # message_id → { title, lines, owners, channel_id }
claims: dict[tuple[int,int], list] = {}     # (message_id, idx) → [User, ...]
request_counter = 0

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
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

# ─── Helpers: whitespace, mojibake fix, structured text extraction, cleaning ──
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def _try_fix_mojibake(s: str) -> str:
    """
    Якщо рядок має ознаки mojibake (символи 'Ð' або 'Ã'), спробувати
    перетворити через latin-1 -> utf-8 або latin-1 -> cp1251 і вибрати
    варіант з найбільшою кількістю кирилиці.
    """
    if not s:
        return s
    if 'Ð' not in s and 'Ã' not in s:
        return s
    candidates = [s]
    try:
        cand = s.encode('latin-1', errors='replace').decode('utf-8', errors='replace')
        candidates.append(cand)
    except Exception:
        pass
    try:
        cand = s.encode('latin-1', errors='replace').decode('cp1251', errors='replace')
        candidates.append(cand)
    except Exception:
        pass

    def cyrillic_score(x: str) -> int:
        return len(re.findall(r'[\u0400-\u04FF]', x))
    best = max(candidates, key=cyrillic_score)
    return best

def extract_structured_text(raw: str) -> str:
    """
    Витягує inner text з <t ...>...</t> (усі вхождення), декодує HTML-ентіті,
    видаляє інші теги і control-символи. Якщо парних тегів немає — прибирає відкривальні теги.
    """
    if not raw:
        return ""
    s = html.unescape(raw)
    # знайти всі парні теги <t ...>...</t> і склеїти їхній innerText
    chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if chunks:
        inner = " ".join(chunks)
        inner = re.sub(r'<[^>]+>', ' ', inner)
        inner = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', inner)
        inner = re.sub(r'\s{2,}', ' ', inner).strip(' "\'').strip()
        inner = _try_fix_mojibake(inner)
        return inner
    # якщо парних тегів немає — видалити відкривальні/закривальні теги як шум
    s = re.sub(r'<\s*t\b[^>]*>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'</\s*t\s*>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    s = _try_fix_mojibake(s)
    return s

def clean_slot_value(raw: str) -> str:
    """
    Агресивне очищення одного токена:
    - витягує inner text з тегів;
    - прибирає шляхи, .sqf/.pbo, довгі аддон-шляхи, control-символи;
    - якщо результат порожній або очевидно шумний — повертає 'Слот'.
    """
    if raw is None:
        return "Слот"
    s = extract_structured_text(raw)
    s = s.replace('\\', '/')
    s = re.sub(r'\.sqf\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\.pbo\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bhttps?://\S+\b', '', s)
    s = re.sub(r'\b[A-Za-z0-9_\\/:.-]{40,}\b', ' ', s)  # довгі шляхи/рядки
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    if not s or s.lower().startswith("<t color") or s in {'"', "'"}:
        return "Слот"
    return s or "Слот"

# ─── RAP decoder (агресивний, з підтримкою cp1251 та mojibake-fix) ──────────
def rap_to_text_aggressive(data: bytes) -> str:
    """
    Heuristic RAP decoder:
    - пробує utf-8, cp1251, latin-1 (в такому порядку);
    - виправляє mojibake;
    - прибирає control-символи;
    - збирає фрагменти навколо ключових слів;
    - нормалізує для парсера.
    """
    enc_tried = None
    text = None
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(enc)
            enc_tried = enc
            break
        except Exception:
            continue
    if text is None:
        text = data.decode("latin-1", errors="ignore")
        enc_tried = "latin-1"

    text = _try_fix_mojibake(text)
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
    return f"// rap_decoded (encoding={enc_tried})\n{candidate}"

# ─── Flexible parser with DOTALL and fallbacks ────────────────────────────────
def parse_mission_sqm_flexible(text: str) -> List[Tuple[str, List[str]]]:
    """
    Повертає список (group_name, [slot1, slot2, ...])
    1) шукає повні 'class Group { ... };' блоки (DOTALL)
    2) інакше — groupName/name/title + вікно unit-токенів (DOTALL)
    3) фолбек — всі unitName/description/text згруповані по маркерах
    """
    groups: List[Tuple[str, List[str]]] = []
    txt = text.replace('\r\n', '\n')

    # 1) Повні блоки class Group
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
                # багаторядкові description/unitName/text
                mslot = re.search(r'(?:description|unitName|text)\s*=\s*"(.*?)"\s*;', u, flags=re.IGNORECASE | re.DOTALL)
                if not mslot:
                    mslot = re.search(r'(?:description|unitName|text)\s*=\s*[\'"](.*?)[\'"]', u, flags=re.IGNORECASE | re.DOTALL)
                if mslot:
                    slots.append(clean_slot_value(mslot.group(1)))
                    continue
                # масиви text[] = { "a", "b" }
                arr = re.findall(r'(?:description|unitName|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', u, flags=re.IGNORECASE | re.DOTALL)
                if arr:
                    items = re.findall(r'[\'"](.+?)[\'"]', arr[0], flags=re.DOTALL)
                    for it in items:
                        slots.append(clean_slot_value(it))
                    continue
                # fallback: name
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

    # 2) groupName/name/title + window
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

    # 3) Фолбек: зібрати всі unit-токени і згрупувати по маркерах
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

# ─── PBO extraction & attachment reading ───────────────────────────────────────
def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    # optional pbo library support; fallback to zip
    try:
        import pbo as pbo_lib
    except Exception:
        pbo_lib = None
    if pbo_lib:
        try:
            archive = pbo_lib.PBO(io.BytesIO(pbo_bytes))
            for name in archive.list():
                if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                    return archive.read(name)
        except Exception:
            pass
    try:
        z = zipfile.ZipFile(io.BytesIO(pbo_bytes))
        for name in z.namelist():
            if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                return z.read(name)
    except Exception:
        pass
    return None

async def read_attachment_sqm_text(attachment: discord.Attachment) -> Tuple[str, str]:
    """
    Читає вкладення (.pbo або .sqm) і повертає (text, method).
    Спроби декодування: utf-8 -> cp1251 -> latin-1.
    Для .pbo намагається витягти mission.sqm з архіву.
    """
    data = await attachment.read()
    filename = attachment.filename.lower()

    def try_decodes(b: bytes):
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                txt = b.decode(enc)
                txt = _try_fix_mojibake(txt)
                return txt, enc
            except Exception:
                continue
        return b.decode("latin-1", errors="ignore"), "latin-1-fallback"

    if filename.endswith(".pbo"):
        sqm_raw = extract_mission_from_pbo_bytes(data)
        if not sqm_raw:
            text = rap_to_text_aggressive(data)
            return text, "pbo-rap-fragments"
        if sqm_raw[:3] == b'raP':
            text = rap_to_text_aggressive(sqm_raw)
            return text, "pbo-rap"
        text, enc = try_decodes(sqm_raw)
        return text, f"pbo-{enc}"

    if filename.endswith(".sqm"):
        if data[:3] == b'raP':
            text = rap_to_text_aggressive(data)
            return text, "rap"
        text, enc = try_decodes(data)
        return text, enc

    if data[:3] == b'raP':
        return rap_to_text_aggressive(data), "rap-raw"
    text, enc = try_decodes(data)
    return text, enc

# ─── Import command with extended diagnostics ──────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]
    try:
        text, method = await read_attachment_sqm_text(attachment)
    except Exception as e:
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    # --- ДІАГНОСТИКА: перші raw-захоплення різними regex-ами ---
    raw_double = re.findall(r'(?:description|unitName|text)\s*=\s*"(.*?)"\s*;', text, flags=re.IGNORECASE | re.DOTALL)[:20]
    raw_single = re.findall(r"(?:description|unitName|text)\s*=\s*'(.*?)'\s*;", text, flags=re.IGNORECASE | re.DOTALL)[:20]
    raw_arrays = re.findall(r'(?:description|unitName|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', text, flags=re.IGNORECASE | re.DOTALL)[:10]
    raw_nq = re.findall(r'(?:description|unitName|text)\s*=\s*([^;{][^;]{1,400})\s*;', text, flags=re.IGNORECASE | re.DOTALL)[:20]
    raw_t_open = re.findall(r'<\s*t\b[^>]*>', text, flags=re.IGNORECASE)[:20]
    raw_t_inner = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', text, flags=re.IGNORECASE | re.DOTALL)[:20]

    dbg_lines = []
    def short(x): return (x[:300] + '...') if x and len(x) > 300 else (x or '')
    if raw_double:
        dbg_lines.append("raw_double[0] = " + short(raw_double[0]))
    if raw_single:
        dbg_lines.append("raw_single[0] = " + short(raw_single[0]))
    if raw_arrays:
        dbg_lines.append("raw_arrays[0] = " + short(raw_arrays[0]))
    if raw_nq:
        dbg_lines.append("raw_no_quotes[0] = " + short(raw_nq[0]))
    if raw_t_open:
        dbg_lines.append("first <t ...> tag = " + short(raw_t_open[0]))
    if raw_t_inner:
        dbg_lines.append("first <t>inner = " + short(raw_t_inner[0]))

    cleaned_examples = []
    if raw_double:
        cleaned_examples.append(clean_slot_value(raw_double[0]))
    elif raw_single:
        cleaned_examples.append(clean_slot_value(raw_single[0]))
    elif raw_arrays:
        arr_items = re.findall(r'[\'"](.+?)[\'"]', raw_arrays[0], flags=re.DOTALL)
        if arr_items:
            cleaned_examples.append(clean_slot_value(arr_items[0]))
    elif raw_nq:
        cleaned_examples.append(clean_slot_value(raw_nq[0]))

    emb_dbg = discord.Embed(title="🔍 Діагностика імпорту mission.sqm", color=discord.Color.orange())
    emb_dbg.add_field(name="Метод обробки", value=method, inline=False)
    if dbg_lines:
        emb_dbg.add_field(name="Приклади raw-захоплень", value="\n".join(dbg_lines)[:1000], inline=False)
    if cleaned_examples:
        emb_dbg.add_field(name="Приклад очищення", value=cleaned_examples[0][:1000], inline=False)
    try:
        await ctx.send(embed=emb_dbg)
    except:
        pass

    # --- основний парсинг ---
    groups = parse_mission_sqm_flexible(text)

    # фолбек: зібрати всі unit-токени
    if not groups:
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"', text, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            units = re.findall(r"(?:unitName|description|text)\s*=\s*'(.*?)'", text, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', text, flags=re.IGNORECASE | re.DOTALL)
            for a in arrs:
                units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
        if not units:
            units = re.findall(r'(?:unitName|description|text)\s*=\s*([^;{][^;]{1,400})\s*;', text, flags=re.IGNORECASE | re.DOTALL)
        if units:
            groups = [("Відділення", [clean_slot_value(u) for u in units])]

    if not groups:
        preview = "\n".join(text.splitlines()[:150])[:1900]
        emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.red())
        emb.add_field(name="Метод обробки", value=method, inline=False)
        emb.add_field(name="Прев'ю початку файлу", value=f"```{preview}```", inline=False)
        try:
            buf = io.BytesIO(text.encode('utf-8'))
            buf.seek(0)
            file = discord.File(fp=buf, filename="decoded_mission_sqm.txt")
            await ctx.send(embed=emb, file=file)
        except Exception:
            await ctx.send(embed=emb)
        return

    target_ch = bot.get_channel(ADMIN_CHANNEL_ID)
    if not target_ch:
        return await ctx.send("❌ Адмін-канал не знайдено за ID.")

    sent_count = 0
    total_slots = 0
    for group_name, slot_list in groups:
        cleaned_slots = []
        for s in slot_list:
            s2 = clean_slot_value(s)
            if not s2 or s2.lower().startswith("<t color") or s2 in {'"', "'"}:
                s2 = "Слот"
            cleaned_slots.append(s2)
        cleaned_slots = [x for x in cleaned_slots if x][:25]
        total_slots += len(cleaned_slots)
        embed = discord.Embed(title=group_name or "Відділення", color=discord.Color.blurple())
        embed.description = "\n".join(f"{i+1}. {s}" for i, s in enumerate(cleaned_slots)) if cleaned_slots else "— слотів не знайдено —"
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

# ─── on_ready / on_message / service commands ─────────────────────────────────
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
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановлено")
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
