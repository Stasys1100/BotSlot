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
    # remove leading/trailing quotes and trailing semicolons and stray double quotes
    s = re.sub(r'^[\'"]+', '', s)
    s = re.sub(r'[\'"]+$', '', s)
    s = re.sub(r';+$', '', s)
    s = s.strip()
    return s

def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    # prefer explicit attributes value/description and <t> tags
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
    # fallback: strip tags and control chars
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    return s

def looks_like_code_block(s: str) -> bool:
    # рядки, що явно містять код/умови/ініціалізацію — відкидаємо
    if not s:
        return True
    if re.search(r'\b(condition|expression|init|compile|preprocessfilelinenumbers|thislist|playerSide|vehicle player)\b', s, flags=re.IGNORECASE):
        return True
    if re.search(r'\\n|\\r|\\t|"\s*\n', s):
        return True
    # якщо рядок містить багато символів коду (скобки, ==, ||, &&) — це код
    if re.search(r'[{}()\[\];=<>!|&\\]', s):
        # але якщо є багато букв і пробілів — можливо це текст з кодом, залишимо для подальшої перевірки
        if len(re.findall(r'[A-Za-zА-Яа-яЁёЇїІіЄєҐґ]', s)) < 5:
            return True
    return False

def clean_line_for_slot(s: str) -> str:
    s = strip_quotes_semicolons(s)
    s = extract_structured_text(s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    # remove leading numbering like '1.' if present
    s = re.sub(r'^\s*\d+\.\s*', '', s)
    # remove stray 'value=' or 'description=' prefixes left
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

# ─── Core parser ─────────────────────────────────────────────────────────────
def extract_units_and_slots(text: str) -> List[Tuple[str, List[str]]]:
    """
    Агресивний парсер: повертає список (title, [slot1, slot2, ...]).
    Фокус: відкинути код/умови, витягнути людські заголовки і їхні слоти.
    """
    # normalize newlines
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    raw_lines = [ln.rstrip() for ln in text.splitlines()]

    # quick cleanup: remove empty and pure-control lines
    raw_lines = [ln for ln in raw_lines if ln and not re.fullmatch(r'[\x00-\x1f\x7f-\x9f]+', ln)]

    # remove long initial addon/class block if present (many lines without spaces or with underscores)
    i = 0
    n = len(raw_lines)
    tech_count = 0
    while i < n and (len(raw_lines[i].split()) == 1 and re.search(r'[_A-Za-z0-9]', raw_lines[i])):
        tech_count += 1
        i += 1
    if tech_count >= 6:
        # skip that initial block
        raw_lines = raw_lines[i:]

    # collapse repeated code blocks (like many lines of condition=" ... " repeated)
    collapsed: List[str] = []
    prev = None
    for ln in raw_lines:
        ln_stripped = ln.strip()
        # if line looks like code, skip it
        if looks_like_code_block(ln_stripped):
            # but if it contains human text inside quotes, extract that
            m = re.search(r'(?:value|description)\s*=\s*"([^"]+)"', ln_stripped, flags=re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                if not looks_like_code_block(candidate):
                    collapsed.append(candidate)
            continue
        # remove trailing '";' or stray quotes
        ln_clean = strip_quotes_semicolons(ln_stripped)
        # avoid adding exact duplicates in a row
        if ln_clean == prev:
            continue
        collapsed.append(ln_clean)
        prev = ln_clean

    # also extract any value/description attributes anywhere
    for m in re.finditer(r'(?:value|description)\s*=\s*"([^"]+)"', text, flags=re.IGNORECASE):
        v = m.group(1).strip()
        if v and not looks_like_code_block(v):
            collapsed.append(v)

    # now cleaned lines to analyze
    lines = [l for l in collapsed if l and not looks_like_code_block(l)]

    # group detection
    groups_map: Dict[str, List[str]] = {}  # title -> slots
    current_title = None

    idx = 0
    L = len(lines)
    while idx < L:
        ln = lines[idx].strip()
        # if line looks like a title: contains '|' or '@' or keywords
        if '|' in ln or '@' in ln or re.search(r'\b(відділен|відділ|бригада|екіпаж|командир|пехотн|піхотн|пехотное|штаб)\b', ln, flags=re.IGNORECASE):
            current_title = normalize_slot_name(strip_quotes_semicolons(ln))
            if current_title not in groups_map:
                groups_map[current_title] = []
            # collect following numbered slots
            j = idx + 1
            while j < L and re.match(r'^\s*\d+\.\s*', lines[j]):
                slot = clean_line_for_slot(lines[j])
                if slot and not looks_like_code_block(slot):
                    groups_map[current_title].append(normalize_slot_name(slot))
                j += 1
            # if none found, try to parse inline numbered items in the same line or next few lines
            if not groups_map[current_title]:
                # inline in same line after title
                inline_found = re.findall(r'\d+\.\s*([^0-9]+?)(?=(?:\d+\.|$))', ln)
                for f in inline_found:
                    f2 = normalize_slot_name(f.strip())
                    if f2 and not looks_like_code_block(f2):
                        groups_map[current_title].append(f2)
                # look ahead few lines for numbered patterns
                k = idx + 1
                while k < min(L, idx + 6):
                    if re.search(r'\d+\.', lines[k]):
                        found = re.findall(r'\d+\.\s*([^0-9]+?)(?=(?:\d+\.|$))', lines[k])
                        for f in found:
                            f2 = normalize_slot_name(f.strip())
                            if f2 and not looks_like_code_block(f2):
                                groups_map[current_title].append(f2)
                    k += 1
            idx += 1
            continue

        # if line starts with numbering -> group under generic title
        if re.match(r'^\s*\d+\.\s*', ln):
            # create generic title if none
            if current_title is None:
                current_title = "Відділення"
                if current_title not in groups_map:
                    groups_map[current_title] = []
            # collect consecutive numbered slots
            while idx < L and re.match(r'^\s*\d+\.\s*', lines[idx]):
                slot = clean_line_for_slot(lines[idx])
                if slot and not looks_like_code_block(slot):
                    groups_map[current_title].append(normalize_slot_name(slot))
                idx += 1
            continue

        # if line contains both title and numbered items in one line (title | 1. A 2. B)
        if '|' in ln and re.search(r'\d+\.', ln):
            parts = ln.split('|', 1)
            title = normalize_slot_name(strip_quotes_semicolons(parts[0]))
            rest = parts[1]
            found = re.findall(r'\d+\.\s*([^0-9]+?)(?=(?:\d+\.|$))', rest)
            slots = [normalize_slot_name(f.strip()) for f in found if f.strip() and not looks_like_code_block(f.strip())]
            if slots:
                groups_map.setdefault(title, []).extend(slots)
            idx += 1
            continue

        # otherwise: maybe a standalone slot-like line (contains Cyrillic words and not too short)
        if re.search(r'[А-Яа-яЁёЇїІіЄєҐґA-Za-z]', ln) and len(ln.split()) >= 1 and not looks_like_code_block(ln):
            # if current_title exists, append as slot; else create generic title
            if current_title is None:
                current_title = "Відділення"
                groups_map.setdefault(current_title, [])
            # avoid adding lines that look like single tokens (technical)
            if len(ln) > 2 and not re.fullmatch(r'[_A-Za-z0-9\-]+', ln):
                groups_map[current_title].append(normalize_slot_name(ln))
        idx += 1

    # postprocess: dedupe slots per title and merge identical titles
    final: List[Tuple[str, List[str]]] = []
    for title, slots in groups_map.items():
        cleaned = []
        seen = set()
        for s in slots:
            s2 = normalize_slot_name(s)
            if not s2:
                continue
            key = s2.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(s2)
        if cleaned:
            final.append((re.sub(r'\s{2,}', ' ', title).strip(), cleaned))

    # merge groups with same normalized title (case-insensitive)
    merged: Dict[str, List[str]] = {}
    for title, slots in final:
        key = title.lower()
        merged.setdefault(key, {"title": title, "slots": []})
        merged[key]["slots"].extend(slots)
    result: List[Tuple[str, List[str]]] = []
    for k, v in merged.items():
        slots = dedupe_preserve_order([s for s in v["slots"] if s and not is_template_slot(s)])
        if slots:
            result.append((v["title"], slots))
    return result

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

    # reset stop flags for this channel
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

    # fallback: if nothing found, try to extract all numbered lines
    if not groups:
        all_numbered = re.findall(r'^\s*\d+\.\s*(.+)$', sqm_text, flags=re.MULTILINE)
        cleaned = []
        for s in all_numbered:
            s2 = clean_line_for_slot(s)
            if s2 and not looks_like_code_block(s2):
                cleaned.append(normalize_slot_name(s2))
        cleaned = dedupe_preserve_order([c for c in cleaned if c and not is_template_slot(c)])
        if cleaned:
            groups = [("Відділення", cleaned)]

    if not groups:
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # send groups sequentially, check stop flags
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
            # chunk fallback
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

    # reset stop flags
    _stop_sending_by_channel[ctx.channel.id] = False
    _stop_sending_global = False

    await ctx.send(f"✅ Готово. Опубліковано відділень: {sent}.")

# ─── UI: slot buttons and claim flow (kept from previous implementation) ─────
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
