import os
import re
import subprocess
import aiohttp
import datetime
import io
import html
import difflib
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict, Any

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive та ENV ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та ініціалізація бота ───────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ           = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID    = 1160843618433630228
ADMIN_CHANNEL_ID  = 1395065909185478769

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}              # message_id → { title, lines, owners, channel_id }
claims: dict[tuple[int,int], list] = {}     # (message_id, idx) → [User, ...]
request_counter = 0                         # лічильник заявок

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Щотижневий нагадувач VTG ────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except Exception:
                pass

# ─── 5. Генератор Embed для слотів ─────────────────────────────────────────────
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

# ─── Допоміжні функції для парсингу/очищення дебінаризованого SQM ─────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def extract_structured_text(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    # try to extract <t> inner content first
    chunks = re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', s, flags=re.IGNORECASE | re.DOTALL)
    if chunks:
        inner = " ".join(chunks)
        inner = re.sub(r'<[^>]+>', ' ', inner)
        inner = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', inner)
        inner = re.sub(r'\s{2,}', ' ', inner).strip(' "\'').strip()
        return inner
    # fallback: strip tags
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    return s

def clean_slot_value(raw: str) -> str:
    if raw is None:
        return "Слот"
    s = extract_structured_text(raw)
    s = s.replace('\\', '/')
    s = re.sub(r'\.sqf\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\.pbo\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bhttps?://\S+\b', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'').strip()
    if not s or s.lower().startswith("<t color") or s in {'"', "'"}:
        return "Слот"
    return s or "Слот"

def split_combined_slot(s: str) -> List[str]:
    """
    Розбиває склеєні рядки на окремі фрази/речення.
    Роздільники: '.', '!', '?', '\n', подвійні пробіли.
    Додатково: розбиває тільки коли після пробілу йде велика літера (щоб не різати абревіатури).
    """
    if not s:
        return []
    s = s.strip()
    s = re.sub(r'[\r\t]+', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s)
    parts = []
    for chunk in re.split(r'\n+|\r+|\s{2,}', s):
        chunk = chunk.strip()
        if not chunk:
            continue
        subparts = re.split(r'(?<=[\.\!\?])\s+(?=[А-ЯІЇЄҐA-Z])', chunk)
        for sp in subparts:
            sp = sp.strip()
            if not sp:
                continue
            if re.search(r'[\.\!\?].*[\.\!\?]', sp):
                more = re.split(r'(?<=[\.\!\?])\s*', sp)
                for m in more:
                    m = m.strip()
                    if m:
                        parts.append(m)
            else:
                parts.append(sp)
    cleaned = []
    for p in parts:
        p = p.strip()
        p = re.sub(r'([!?.]){2,}', r'\1', p)
        p = re.sub(r'\s+([!?.,:;])', r'\1', p)
        p = p.strip(" \t\n\r")
        if p:
            cleaned.append(p)
    return cleaned

def normalize_slot_name(s: str) -> str:
    s = s or ""
    s = s.strip()
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'\s+([!?.,:;])', r'\1', s)
    s = re.sub(r'([!?.]){2,}', r'\1', s)
    s = s.strip(" \t\n\r-–—")
    return s

def is_template_slot(s: str) -> bool:
    if not s:
        return True
    low = s.lower().strip()
    if low in ("слот", "-", "—", "n/a", "none", "пусто"):
        return True
    if re.fullmatch(r'^[\W_]{1,5}$', low):
        return True
    return False

def dedupe_preserve_order(items: List[str], fuzzy_threshold: float = 0.78) -> List[str]:
    """
    Зберігає порядок, зливає дуже схожі рядки (fuzzy similarity >= fuzzy_threshold).
    Пріоритет: більше кирилиці -> довший рядок.
    """
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

# ─── 6. SlotButton та SlotView ─────────────────────────────────────────────────
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

        # 6.1) Вільний слот → зайняти одразу (1 слот/гілка)
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # 6.2) Свій слот → звільнити
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # 6.3) Чужий слот → ефермерно пропонуємо “Претендувати”
        return await inter.response.send_message(
            f"⚠️ Цей слот зайнято {owner.mention}.",
            view=ClaimSlotView(self.sid, self.idx),
            ephemeral=True
        )

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# ─── 7. “Претендувати” на слот ─────────────────────────────────────────────────
class ClaimSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(
            label="❗ Претендувати",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim-slot-{sid}-{idx}"
        )
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]

        # заборона претендування, якщо користувач вже в слоті в цій гілці
        for s in sessions.values():
            if s["channel_id"] == sess["channel_id"] and user in s["owners"]:
                return await inter.response.send_message(
                    "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                )

        key = (self.sid, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message(
                "ℹ️ Ви вже подали заявку.", ephemeral=True
            )
        lst.append(user)
        await inter.response.send_message("✅ Заявка прийнята.", ephemeral=True)

        # нотифікуємо адміністраторів
        global request_counter
        request_counter += 1
        embed = discord.Embed(
            title=f"📝 Заявка #{request_counter}",
            description=sess["title"],
            color=discord.Color.orange()
        )
        embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
        embed.add_field(
            name="Власник",
            value=(sess["owners"][self.idx].mention if sess["owners"][self.idx] else "Вільний"),
            inline=True
        )
        embed.add_field(name="Кандидат", value=user.mention, inline=False)

        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            msg = await admin_ch.send(embed=embed)
            await msg.edit(view=ClaimDecisionView(self.sid, self.idx, user.id, msg.id))

class ClaimSlotView(View):
    def __init__(self, sid: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(sid, idx))

# ─── 8. Modal для рішення (призначення/відмови) ─────────────────────────────────
class DecisionModal(Modal):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int,
        accept: bool
    ):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.sid = sid
        self.idx = idx
        self.claimant_id = claimant_id
        self.admin_msg_id = admin_msg_id
        self.accept = accept
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

        # оновлюємо головне повідомлення
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass

        # надсилаємо DM
        try:
            if self.accept:
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\n"
                    f"Причина: {reason}"
                )
                if old_owner and old_owner != claimant:
                    await old_owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\n"
                        f"Причина: {reason}"
                    )
            else:
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\n"
                    f"Причина: {reason}"
                )
        except: pass

        # видаляємо адмін-повідомлення
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            try:
                admin_msg = await admin_ch.fetch_message(self.admin_msg_id)
                await admin_msg.delete()
            except: pass

        await inter.response.send_message("✔️ Готово.", ephemeral=True)

class ClaimDecisionButton(Button):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int,
        accept: bool
    ):
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(
            label=label,
            style=style,
            custom_id=f"dec-{tag}-{sid}-{idx}-{claimant_id}-{admin_msg_id}"
        )
        self.sid = sid
        self.idx = idx
        self.claimant_id = claimant_id
        self.admin_msg_id = admin_msg_id
        self.accept = accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(
            self.sid,
            self.idx,
            self.claimant_id,
            self.admin_msg_id,
            self.accept
        )
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int
    ):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, False))

# ─── 9. Команди та події для зняття зі слоту ────────────────────────────────────
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
            return await inter.response.send_message(
                f"⚠️ Слот #{self.idx+1} вже вільний.", ephemeral=True
            )

        # звільнення слоту
        sess["owners"][self.idx] = None

        # оновлюємо головне повідомлення
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass

        # повідомляємо колишнього власника
        try:
            await owner.send(
                f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\n"
                f"Причина: {reason}"
            )
        except: pass

        await inter.response.send_message(
            f"✅ Слот #{self.idx+1} звільнено.", ephemeral=True
        )

class RemoveSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(
            label=str(idx+1),
            style=discord.ButtonStyle.danger,
            custom_id=f"remove-{sid}-{idx}"
        )
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
    await ctx.send(
        f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:",
        view=RemoveSlotView(session_msg_id)
    )

# ─── 10. Події on_ready, on_message та інші команди ─────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    for guild in bot.guilds:
        ch = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
                      and c.permissions_for(guild.me).send_messages,
            guild.text_channels
        )
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
                owner = next(
                    (u for u in message.mentions
                     if f"<@{u.id}>" in txt or f"<@!{u.id}>" in txt),
                    None
                )
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = txt

        slots, owners = slots[:25], owners[:len(slots)]
        sess = {
            "title":      header or DEFAULT_TITLE,
            "lines":      slots,
            "owners":     owners,
            "channel_id": message.channel.id
        }
        embed = build_embed(sess)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── 11. Команда: імпорт вже дебінаризованого mission.sqm ─────────────────────
@bot.command(name="імпорт_sqm_decoded", aliases=["import_sqm_decoded"])
async def імпорт_sqm_decoded(ctx: commands.Context):
    """
    Приймає прикріплений вже дебінаризований mission.sqm (plain text)
    і повертає тільки відділення та слоти у plain-text блоці.
    """
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть текстовий mission.sqm до повідомлення.")
    attachment = ctx.message.attachments[0]

    # прочитати як текст (припускаємо, що файл вже дебінаризований)
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

    groups = []
    txt = sqm_text.replace('\r\n', '\n')

    # знайти блоки class Group/Section/Unit
    block_pattern = re.compile(r'(class\s+(?:Group|Section|Unit|Side|Faction)\b.*?\{.*?\}\s*;?)', flags=re.IGNORECASE | re.DOTALL)
    blocks = block_pattern.findall(txt)
    if blocks:
        for blk in blocks:
            mname = re.search(r'(?:name|groupName|title)\s*=\s*(?P<val>"[^"]*"|\'[^\']*\'|[^\s;]+)\s*;', blk, flags=re.IGNORECASE)
            if mname:
                raw_name = mname.group('val').strip().strip('"\'')
                gname = normalize_slot_name(clean_slot_value(raw_name))
            else:
                gname = "Відділення"

            slots = []
            for m in re.finditer(r'(?:unitName|description|text|name)\s*=\s*"(.*?)"\s*;', blk, flags=re.IGNORECASE | re.DOTALL):
                slots.append(m.group(1))
            for m in re.finditer(r"(?:unitName|description|text|name)\s*=\s*'(.*?)'\s*;", blk, flags=re.IGNORECASE | re.DOTALL):
                slots.append(m.group(1))
            for arr in re.finditer(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', blk, flags=re.IGNORECASE | re.DOTALL):
                inner = arr.group(1)
                items = re.findall(r'[\'"](.+?)[\'"]', inner, flags=re.DOTALL)
                for it in items:
                    slots.append(it)
            for ti in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', blk, flags=re.IGNORECASE | re.DOTALL):
                slots.append(ti)

            cleaned_slots = []
            for s in slots:
                s_clean = clean_slot_value(s)
                parts = split_combined_slot(s_clean)
                for p in parts:
                    p2 = normalize_slot_name(p)
                    if p2 and not is_template_slot(p2):
                        cleaned_slots.append(p2)
            cleaned_slots = dedupe_preserve_order(cleaned_slots)
            if cleaned_slots:
                groups.append((gname, cleaned_slots))
    else:
        # глобальний пошук
        slots_global = []
        for m in re.finditer(r'(?:unitName|description|text|name)\s*=\s*"(.*?)"\s*;', txt, flags=re.IGNORECASE | re.DOTALL):
            slots_global.append(m.group(1))
        for m in re.finditer(r"(?:unitName|description|text|name)\s*=\s*'(.*?)'\s*;", txt, flags=re.IGNORECASE | re.DOTALL):
            slots_global.append(m.group(1))
        for ti in re.findall(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', txt, flags=re.IGNORECASE | re.DOTALL):
            slots_global.append(ti)
        if slots_global:
            cleaned = []
            for s in slots_global:
                s_clean = clean_slot_value(s)
                for p in split_combined_slot(s_clean):
                    p2 = normalize_slot_name(p)
                    if p2 and not is_template_slot(p2):
                        cleaned.append(p2)
            cleaned = dedupe_preserve_order(cleaned)
            groups.append(("Відділення", cleaned))

    if not groups:
        return await ctx.send("⚠️ Не знайдено відділень або слотів у цьому файлі.")

    # сформувати plain-text вихід (заголовок + нумерований список)
    outputs = []
    for title, slots in groups:
        lines = [re.sub(r'\s{2,}', ' ', title).strip()]
        for i, s in enumerate(slots, start=1):
            lines.append(f"{i}. {s}")
        outputs.append("\n".join(lines))

    # надіслати кожне відділення як окремий кодовий блок
    for out in outputs:
        try:
            await ctx.send(f"```{out}```")
        except:
            parts = out.splitlines()
            chunk = []
            for i, line in enumerate(parts):
                chunk.append(line)
                if (i+1) % 50 == 0:
                    await ctx.send(f"```{chr(10).join(chunk)}```")
                    chunk = []
            if chunk:
                await ctx.send(f"```{chr(10).join(chunk)}```")

    await ctx.send(f"✅ Готово. Опубліковано відділень: {len(outputs)}.")

# ─── 11. Сервісні команди ───────────────────────────────────────────────────────
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
    await ctx.send(
        f"🧠 Commit: `{commit}`\n"
        f"📊 Sessions: {len(sessions)}\n"
        f"📋 Claims: {sum(len(v) for v in claims.values())}"
    )

@bot.command(name="gitpush")
async def _gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                         inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 12. Запуск бота ─────────────────────────────────────────────────────────────
if not TOKEN:
    print("DISCORD_TOKEN not set in environment")
else:
    bot.run(TOKEN)
