# bot.py
import os
import re
import subprocess
import aiohttp
import datetime
import io
import zipfile
import html
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

# Optional keep-alive (remove if unused)
try:
    from keep_alive import keep_alive
    keep_alive()
except Exception:
    pass

# Optional PBO library (if installed). If not — pbo = None
try:
    import pbo
except Exception:
    pbo = None

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
sessions: dict[int, dict] = {}
claims: dict[tuple[int,int], list] = {}
request_counter = 0

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── Reminder ────────────────────────────────────────────────────────────────
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

# ─── Embeds ───────────────────────────────────────────────────────────────────
def build_group_embed(title: str, slots: List[str]) -> discord.Embed:
    embed = discord.Embed(title=title or "Відділення", color=discord.Color.blurple())
    if slots:
        lines = [f"{i+1}. {s}" for i, s in enumerate(slots)]
        embed.description = "\n".join(lines)
    else:
        embed.description = "— слотів не знайдено —"
    return embed

# ─── Utilities: cleaning, HTML stripping, normalization ────────────────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def _strip_html_tags(s: str) -> str:
    # remove tags like <t color='#fff'>text</t> or <t color=...> and keep inner text
    s = re.sub(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', r'\1', s, flags=re.IGNORECASE | re.DOTALL)
    # remove any remaining tags
    s = re.sub(r'<[^>]+>', ' ', s)
    return s

def _clean_token(tok: str) -> str:
    if not tok:
        return "Слот"
    tok = tok.strip()
    # decode HTML entities
    tok = html.unescape(tok)
    # strip HTML tags and attributes
    tok = _strip_html_tags(tok)
    # remove control chars
    tok = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', tok)
    # normalize slashes and remove file suffixes
    tok = tok.replace('\\', '/')
    tok = re.sub(r'\.sqf\b', '', tok, flags=re.IGNORECASE)
    tok = re.sub(r'\.pbo\b', '', tok, flags=re.IGNORECASE)
    # remove long addon paths or repeated commas
    tok = re.sub(r'\b[A-Za-z0-9_\\/:.-]{30,}\b', '', tok)
    tok = re.sub(r'\s{2,}', ' ', tok)
    tok = tok.strip(' "\'')
    # if token looks like a class name (B_Soldier_AR_F) — keep as-is but replace underscores with spaces optionally
    # keep original class names but make them readable
    if re.match(r'^[A-Za-z0-9_]+$', tok) and '_' in tok:
        tok = tok.replace('_', ' ')
    return tok or "Слот"

# ─── Aggressive RAP -> text decoder ───────────────────────────────────────────
def rap_to_text_aggressive(data: bytes) -> str:
    """
    Heuristic RAP decoder:
    - try utf-8 then latin-1
    - remove control chars
    - keep readable characters
    - collect windows around keywords
    - normalize separators for parser
    """
    try:
        raw = data.decode('utf-8')
        enc = 'utf-8'
    except Exception:
        raw = data.decode('latin-1', errors='ignore')
        enc = 'latin-1'

    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+', ' ', raw)

    def keep(ch):
        o = ord(ch)
        return (32 <= o <= 126) or (0x0400 <= o <= 0x04FF) or ch in '\n\r\t{}=;"\'.,:-_()[]/\\<>|'
    filtered = ''.join(ch if keep(ch) else ' ' for ch in raw)

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
        window = 4000
        frags = []
        for idx in sorted(set(indices)):
            s = max(0, idx - 300)
            e = min(len(filtered), idx + window)
            frags.append(filtered[s:e])
        candidate = '\n'.join(frags)
    else:
        candidate = filtered[:200000]

    candidate = candidate.replace('};', '};\n')
    candidate = re.sub(r';\s*', ';\n', candidate)
    candidate = re.sub(r'\{\s*', '{\n', candidate)
    candidate = re.sub(r'\s*\}\s*', '\n}\n', candidate)

    # clean lines and tokens
    lines = [line.strip() for line in candidate.splitlines()]
    lines = [re.sub(r'\s{2,}', ' ', l) for l in lines if l.strip()]
    candidate = '\n'.join(lines)
    candidate = _normalize_whitespace(candidate)
    candidate = candidate.replace("'", '"')
    return f"// rap_decoded (encoding={enc})\n{candidate}"

# ─── Flexible parser with tolerant regexes and HTML cleanup ──────────────────
def parse_mission_sqm_flexible(text: str) -> List[Tuple[str, List[str]]]:
    """
    Returns list of (group_name, [slot1, slot2, ...])
    Steps:
    1) try to find full 'class Group { ... };' blocks
    2) if none, find groupName/name/title and collect unit tokens in a window
    3) fallback: collect all unitName/description/text tokens and group by nearest group marker
    """
    groups: List[Tuple[str, List[str]]] = []
    txt = text.replace('\r\n', '\n')

    # 1) full class Group blocks (DOTALL)
    group_blocks = re.findall(r'(class\s+Group\b.*?\{.*?\}[\s;]*)', txt, flags=re.IGNORECASE | re.DOTALL)
    if group_blocks:
        for blk in group_blocks:
            mname = re.search(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', blk, flags=re.IGNORECASE)
            gname = _clean_token(mname.group("v")) if mname else "Відділення"
            units = re.findall(r'class\s+Unit\b.*?\{(.*?)\}', blk, flags=re.IGNORECASE | re.DOTALL)
            slots = []
            for u in units:
                # try description/unitName/text first
                mslot = re.search(r'(?:description|unitName|text)\s*=\s*"?(?P<v>[^";\n]+)"?', u, flags=re.IGNORECASE)
                if mslot:
                    slots.append(_clean_token(mslot.group("v")))
                    continue
                # fallback: name inside unit
                mslot2 = re.search(r'(?:name)\s*=\s*"?(?P<v>[^";\n]+)"?', u, flags=re.IGNORECASE)
                if mslot2:
                    slots.append(_clean_token(mslot2.group("v")))
                    continue
                # last resort: try to extract any quoted text
                q = re.search(r'"([^"]{2,200})"', u)
                if q:
                    slots.append(_clean_token(q.group(1)))
                else:
                    slots.append("Слот")
            groups.append((gname, slots))
        return groups

    # 2) groupName/name/title then unit tokens in window
    for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', txt, flags=re.IGNORECASE):
        gname = _clean_token(m.group("v"))
        start = m.end()
        frag = txt[start:start + 30000]
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', frag, flags=re.IGNORECASE)
        if units:
            groups.append((gname, [_clean_token(u) for u in units]))

    if groups:
        return groups

    # 3) fallback: collect all unit tokens and group by nearest group marker
    unit_matches = [(m.start(), _clean_token(m.group("v"))) for m in re.finditer(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', txt, flags=re.IGNORECASE)]
    group_markers = [m.start() for m in re.finditer(r'(?:class\s+Group\b|groupName|name|title)', txt, flags=re.IGNORECASE)]
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
                snippet = txt[key:key+200]
                mname = re.search(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', snippet, flags=re.IGNORECASE)
                gname = _clean_token(mname.group("v")) if mname else "Відділення"
            groups.append((gname, slots))
        return groups

    return []

# ─── PBO extraction & attachment reading ───────────────────────────────────────
def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    if pbo:
        try:
            archive = pbo.PBO(io.BytesIO(pbo_bytes))
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
    data = await attachment.read()
    filename = attachment.filename.lower()

    if filename.endswith(".pbo"):
        sqm_raw = extract_mission_from_pbo_bytes(data)
        if not sqm_raw:
            text = rap_to_text_aggressive(data)
            return text, "pbo-rap-fragments"
        if sqm_raw[:3] == b'raP':
            text = rap_to_text_aggressive(sqm_raw)
            return text, "pbo-rap"
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return sqm_raw.decode(enc), f"pbo-{enc}"
            except Exception:
                continue
        return sqm_raw.decode("latin-1", errors="ignore"), "pbo-latin1-fallback"

    if filename.endswith(".sqm"):
        if data[:3] == b'raP':
            text = rap_to_text_aggressive(data)
            return text, "rap"
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return data.decode(enc), enc
            except Exception:
                continue
        return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

    if data[:3] == b'raP':
        return rap_to_text_aggressive(data), "rap-raw"
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

# ─── Import command: robust parsing, cleaning, diagnostics ────────────────────
@bot.command(name="імпорт_sqm")
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

    groups = parse_mission_sqm_flexible(text)

    diagnostics = {"method": method, "found_groups": 0, "found_unit_tokens": 0, "sample_fragments": []}

    if not groups:
        unit_tokens = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', text, flags=re.IGNORECASE)
        diagnostics["found_unit_tokens"] = len(unit_tokens)
        group_markers = re.findall(r'(?:class\s+Group\b|groupName|name|title)', text, flags=re.IGNORECASE)
        if group_markers and unit_tokens:
            groups = []
            for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', text, flags=re.IGNORECASE):
                gname = _clean_token(m.group("v"))
                frag = text[m.end(): m.end() + 20000]
                units = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', frag, flags=re.IGNORECASE)
                if units:
                    groups.append((gname, [_clean_token(u) for u in units]))
            diagnostics["found_groups"] = len(groups)
        else:
            for kw in ["class Group", "class Unit", "groupName", "unitName", "description", "name"]:
                idx = text.lower().find(kw.lower())
                if idx != -1:
                    s = max(0, idx - 200)
                    e = min(len(text), idx + 800)
                    diagnostics["sample_fragments"].append(text[s:e])
            if unit_tokens:
                groups = [("Відділення", [_clean_token(u) for u in unit_tokens])]
                diagnostics["found_groups"] = 1
    else:
        diagnostics["found_groups"] = len(groups)
        diagnostics["found_unit_tokens"] = sum(len(slots) for _, slots in groups)

    if not groups:
        preview = "\n".join(text.splitlines()[:200])[:1900]
        emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.orange())
        emb.add_field(name="Метод обробки", value=diagnostics["method"], inline=False)
        emb.add_field(name="Знайдено unit tokens", value=str(diagnostics["found_unit_tokens"]), inline=True)
        emb.add_field(name="Знайдено груп (фолбек)", value=str(diagnostics["found_groups"]), inline=True)
        if diagnostics["sample_fragments"]:
            emb.add_field(name="Приклад фрагмента", value=diagnostics["sample_fragments"][0][:1000], inline=False)
        emb.add_field(name="Прев'ю початку файлу", value=f"```{preview}```", inline=False)
        # також прикріпимо повний декодований текст як файл для діагностики
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
        cleaned_slots = [_clean_token(s) for s in slot_list][:25]
        total_slots += len(cleaned_slots)
        embed = build_group_embed(group_name, cleaned_slots)
        try:
            await target_ch.send(embed=embed)
            sent_count += 1
        except Exception:
            pass

    await ctx.send(f"✅ Імпорт завершено. Опубліковано відділень: {sent_count}. Метод: `{method}`. Знайдено слотів: {total_slots}.")

# ─── on_ready / on_message ────────────────────────────────────────────────────
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
        embed = discord.Embed(title=sess["title"], color=discord.Color.blue())
        lines = []
        for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
            prefix = f"{i+1}. "
            if owner:
                lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
            else:
                lines.append(f"{prefix}{text}")
        embed.description = "\n".join(lines)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── Minimal UI classes for slots (kept for compatibility) ────────────────────
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

        return await inter.response.send_message(f"⚠️ Цей слот зайнято {owner.mention}.", ephemeral=True)

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# ─── Run ─────────────────────────────────────────────────────────────────────
if not TOKEN:
    print("DISCORD_TOKEN not set in environment")
else:
    bot.run(TOKEN)
