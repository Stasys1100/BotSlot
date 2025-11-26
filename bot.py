# bot.py
# Discord бот: приймає дебінаризований mission.sqm і повертає тільки відділення + слоти
# .env: DISCORD_TOKEN (обов'язково), ADMIN_CHANNEL_ID (опц.), VTG_CHANNEL_ID (опц.), DEPLOY_HOOK_URL (опц.)

import os
import re
import subprocess
import datetime
import html
import difflib
import asyncio
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict

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

# Ключові слова, що позначають початок списку слотів
SLOT_START_KEYWORDS = [
    r'командир відділен', r'командир сторони', r'командир екіпаж', r'командир',
    r'пілот', r'оператор', r'оператор-навідник', r'наводник', r'санитар',
    r'медик', r'гренадер', r'гранатометник', r'кулеметник', r'стрілець',
    r'механик', r'механік', r'оператор бпла'
]
SLOT_START_RE = re.compile(r'^\s*(?:' + r'|'.join(SLOT_START_KEYWORDS) + r')\b', flags=re.IGNORECASE)

def looks_like_slot_start(line: str) -> bool:
    if not line:
        return False
    line = line.strip()
    if re.match(r'^\s*\d+\.\s*', line):
        return True
    if SLOT_START_RE.search(line):
        return True
    if re.search(r'\b(ENG|MED|CC|SS|СС)\b', line, flags=re.IGNORECASE):
        return True
    return False

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
def strip_quotes_semicolons(s: str) -> str:
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'^[\'"]+', '', s)
    s = re.sub(r'[\'"]+$', '', s)
    s = re.sub(r';+$', '', s)
    s = s.strip()
    return s

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

# ─── Core parser (менш агресивний) ──────────────────────────────────────────
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Менш агресивний парсер: зберігає addon/class блоки, але витягує відділення і слоти.
    Повертає список (title, [slot1, slot2, ...]).
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Нормалізуємо лапки/крапки з комою
    norm_lines = [re.sub(r'[\'"]+$', '', re.sub(r'^[\'"]+', '', ln)).rstrip(';') for ln in lines]

    groups: List[Tuple[str, List[str]]] = []
    i = 0
    L = len(norm_lines)

    title_hint_re = re.compile(r'\|')  # рядки з '|' часто — заголовки
    numbered_re = re.compile(r'^\s*\d+\.\s*')
    slot_start_re = re.compile(r'^\s*(?:командир|командир відділен|командир сторони|командир екіпаж|пілот|оператор|медик|санитар|гренадер|гранатометник|кулеметник|стрілець|механик|механік)', flags=re.IGNORECASE)

    while i < L:
        ln = norm_lines[i]

        # Якщо рядок виглядає як заголовок (має '|', або містить @Альфа, або 'Штаб', 'бригада' тощо)
        if title_hint_re.search(ln) or re.search(r'@Альфа|Штаб|бригада|ОМБр|ЧВК|РСЗВ', ln, flags=re.IGNORECASE):
            title = ln
            slots: List[str] = []
            j = i + 1
            # збираємо наступні нумеровані або явні слот-рядки
            while j < L and (numbered_re.match(norm_lines[j]) or slot_start_re.match(norm_lines[j]) or re.search(r'\b(ENG|MED|CC|СС)\b', norm_lines[j], flags=re.IGNORECASE)):
                s = numbered_re.sub('', norm_lines[j]).strip()
                s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
                s = re.sub(r'[";]+$', '', s).strip()
                if s and not looks_like_code_block(s):
                    slots.append(normalize_slot_name(clean_line_for_slot(s)))
                j += 1
            # якщо не знайшли слоти безпосередньо — спробуємо знайти value/description в наступних 6 рядках
            if not slots:
                k = i + 1
                while k < min(L, i + 7):
                    m = re.search(r'(?:value|description)\s*=\s*"([^"]+)"', norm_lines[k], flags=re.IGNORECASE)
                    if m:
                        candidate = m.group(1).strip()
                        if candidate:
                            slots.append(normalize_slot_name(clean_line_for_slot(candidate)))
                    k += 1
            if slots:
                groups.append((title.strip(), [re.sub(r'\s{2,}', ' ', s).strip() for s in slots]))
                i = j
                continue
            else:
                i += 1
                continue

        # Якщо рядок починається з нумерації або виглядає як слот — зібрати послідовність і створити заголовок "Відділення"
        if numbered_re.match(ln) or slot_start_re.match(ln):
            slots = []
            while i < L and (numbered_re.match(norm_lines[i]) or slot_start_re.match(norm_lines[i]) or re.search(r'\b(ENG|MED|CC|СС)\b', norm_lines[i], flags=re.IGNORECASE)):
                s = numbered_re.sub('', norm_lines[i]).strip()
                s = re.sub(r'^(value|description)\s*=\s*', '', s, flags=re.IGNORECASE)
                s = re.sub(r'[";]+$', '', s).strip()
                if s:
                    slots.append(s)
                i += 1
            if slots:
                groups.append(("Відділення", [re.sub(r'\s{2,}', ' ', s).strip() for s in slots]))
            continue

        # Інакше — просто рухаємось далі
        i += 1

    # очистка: видалити дублікати в кожній групі
    final = []
    for title, slots in groups:
        seen = set()
        cleaned = []
        for s in slots:
            key = s.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(s)
        if cleaned:
            final.append((title, cleaned))
    return final

# ─── Команда: стоп ───────────────────────────────────────────────────────────
@bot.command(name="стоп", aliases=["stop"])
async def стоп(ctx: commands.Context):
    global _stop_sending_global, _stop_sending_by_channel
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    _stop_sending_global = True
    _stop_sending_by_channel[ctx.channel.id] = True
    await ctx.send("⏹️ Зупиняю відправку відділень...")

# ─── Команда: імпорт_sqm_decoded ─────────────────────────────────────────────
@bot.command(name="імпорт_sqm_decoded", aliases=["import_sqm", "import_sqm_decoded", "імпорт_sqm"])
async def імпорт_sqm_decoded(ctx: commands.Context):
    global _stop_sending_global, _stop_sending_by_channel
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть текстовий mission.sqm до повідомлення.")
    attachment = ctx.message.attachments[0]
    _stop_sending_by_channel[ctx.channel.id] = False
    _stop_sending_global = False
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
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', sqm_text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order([c for c in cleaned if c and not re.fullmatch(r'^[\W_]{1,5}$', c)])
        if cleaned:
            groups = [("Відділення", cleaned)]
    if not groups:
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")
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
    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

# ─── UI: slot buttons and claim flow (kept) ─────────────────────────────────
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
