import os
import re
import subprocess
import aiohttp
import datetime
import io
import zipfile
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv
from keep_alive import keep_alive

# Optional PBO library (if installed on host). If not installed — pbo = None
try:
    import pbo
except Exception:
    pbo = None

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
KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID = 1160843618433630228
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or "1395065909185478769")

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}
claims: dict[tuple[int,int], list] = {}
request_counter = 0

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
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
            except:
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

def build_group_embed(title: str, slots: List[str]) -> discord.Embed:
    embed = discord.Embed(title=title or "Відділення", color=discord.Color.blurple())
    if slots:
        lines = [f"{i+1}. {s}" for i, s in enumerate(slots)]
        embed.description = "\n".join(lines)
    else:
        embed.description = "— слотів не знайдено —"
    return embed

# ─── 5.2 ПОКРАЩЕНИЙ RAP → текст декодер ────────────────────────────────────────
def rap_to_text(data: bytes) -> str:
    """
    Покращений RAP декодер з кращою нормалізацією для парсингу.
    """
    text = data.decode("latin-1", errors="ignore")

    def is_readable(ch: str) -> bool:
        o = ord(ch)
        return (32 <= o <= 126) or (0x00A0 <= o <= 0x04FF)

    # Фільтрація нечитаємих символів
    filtered = "".join(ch if is_readable(ch) else " " for ch in text)

    # Агресивна нормалізація для покращення парсингу
    normalized = filtered
    
    # Додаємо переноси рядків перед ключовими словами
    normalized = re.sub(r'(\s+)(class\s+Group)', r'\n\2', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'(\s+)(class\s+Unit)', r'\n\2', normalized, flags=re.IGNORECASE)
    
    # Нормалізуємо дужки
    normalized = normalized.replace("};", "};\n")
    normalized = normalized.replace("{", "{\n")
    normalized = normalized.replace("}", "\n}")
    
    # Нормалізуємо крапку з комою
    normalized = re.sub(r';(\s*[a-zA-Z])', r';\n\1', normalized)
    
    # Видаляємо множинні пробіли
    normalized = re.sub(r'[ \t]+', ' ', normalized)
    
    # Видаляємо множинні порожні рядки
    normalized = re.sub(r'\n\s*\n\s*\n+', '\n\n', normalized)
    
    return normalized

# ─── 5.3 ПОКРАЩЕНИЙ парсер mission.sqm ─────────────────────────────────────────
def parse_mission_sqm(text: str) -> List[Tuple[str, List[str]]]:
    """
    Покращений парсер з більш гнучкими regex та фолбек-логікою.
    """
    groups: List[Tuple[str, List[str]]] = []
    
    # Спроба 1: Структурований парсинг
    try:
        groups = _structured_parse(text)
        if groups:
            return groups
    except Exception:
        pass
    
    # Спроба 2: Агресивний пошук фрагментів
    try:
        groups = _aggressive_parse(text)
        if groups:
            return groups
    except Exception:
        pass
    
    return groups

def _structured_parse(text: str) -> List[Tuple[str, List[str]]]:
    """
    Структурований парсинг з гнучкими regex.
    """
    groups: List[Tuple[str, List[str]]] = []
    current_group_name: Optional[str] = None
    current_slots: List[str] = []
    
    # Більш гнучкі regex
    re_group_start = re.compile(r'class\s+Group', re.IGNORECASE)
    re_group_name = re.compile(
        r'(name|groupName|description|title)\s*=\s*["\']?([^";\'}\n]+)["\']?\s*;',
        re.IGNORECASE
    )
    re_unit_start = re.compile(r'class\s+Unit', re.IGNORECASE)
    re_slot_desc = re.compile(
        r'(description|unitName|text)\s*=\s*["\']?([^";\'}\n]+)["\']?\s*;',
        re.IGNORECASE
    )
    
    in_group = False
    in_unit = False
    brace_depth = 0
    
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        
        # Відстеження глибини дужок
        brace_depth += stripped.count('{') - stripped.count('}')
        
        # Початок групи
        if re_group_start.search(stripped):
            if current_group_name is not None or current_slots:
                groups.append((current_group_name or "Відділення", current_slots))
            current_group_name = None
            current_slots = []
            in_group = True
            in_unit = False
            continue
        
        # Назва групи
        if in_group and current_group_name is None:
            m = re_group_name.search(stripped)
            if m:
                current_group_name = m.group(2).strip()
                continue
        
        # Початок юніта
        if re_unit_start.search(stripped):
            in_unit = True
            current_slots.append("Слот")
            continue
        
        # Опис слота
        if in_unit:
            m = re_slot_desc.search(stripped)
            if m:
                val = m.group(2).strip()
                if val and current_slots:
                    current_slots[-1] = val
                continue
        
        # Закриття блоку
        if '};' in stripped or ('}' in stripped and brace_depth <= 0):
            if in_unit:
                in_unit = False
            elif in_group:
                in_group = False
                if current_group_name is not None or current_slots:
                    groups.append((current_group_name or "Відділення", current_slots))
                current_group_name = None
                current_slots = []
    
    # Додаємо останню групу
    if current_group_name is not None or current_slots:
        groups.append((current_group_name or "Відділення", current_slots))
    
    return groups

def _aggressive_parse(text: str) -> List[Tuple[str, List[str]]]:
    """
    Агресивний пошук груп та слотів без строгої структури.
    """
    groups: List[Tuple[str, List[str]]] = []
    
    # Шукаємо всі можливі назви груп
    group_names = []
    for m in re.finditer(
        r'(?:name|groupName|title)\s*=\s*["\']?([^";\'}\n]+)["\']?',
        text,
        flags=re.IGNORECASE
    ):
        name = m.group(1).strip()
        if name and len(name) > 2:
            group_names.append((m.start(), name))
    
    # Для кожної назви групи шукаємо слоти в околиці
    for idx, (pos, gname) in enumerate(group_names):
        # Визначаємо вікно пошуку (до наступної групи або 5000 символів)
        end_pos = group_names[idx + 1][0] if idx + 1 < len(group_names) else pos + 5000
        window = text[pos:min(end_pos, len(text))]
        
        # Шукаємо описи слотів
        slots = []
        for m in re.finditer(
            r'(?:description|unitName|text)\s*=\s*["\']?([^";\'}\n]+)["\']?',
            window,
            flags=re.IGNORECASE
        ):
            slot_text = m.group(1).strip()
            if slot_text and len(slot_text) > 1:
                slots.append(slot_text)
        
        if slots:
            groups.append((gname, slots[:25]))  # Обмежуємо 25 слотами
    
    return groups

# ─── 5.4 Витяг з PBO та читання вкладень ───────────────────────────────────────
def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    """
    Повертає raw bytes mission.sqm з PBO або None.
    """
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
    """
    Повертає (text, method).
    """
    data = await attachment.read()
    filename = attachment.filename.lower()

    # .pbo: витягнути mission.sqm
    if filename.endswith(".pbo"):
        sqm_raw = extract_mission_from_pbo_bytes(data)
        if not sqm_raw:
            text = rap_to_text(data)
            return text, "pbo-rap-fragments"
        
        if sqm_raw[:3] == b'raP':
            text = rap_to_text(sqm_raw)
            return text, "pbo-rap"
        
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return sqm_raw.decode(enc), f"pbo-{enc}"
            except Exception:
                continue
        return sqm_raw.decode("latin-1", errors="ignore"), "pbo-latin1-fallback"

    # .sqm: визначити RAP чи текст
    if filename.endswith(".sqm"):
        if data[:3] == b'raP':
            text = rap_to_text(data)
            return text, "rap"
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return data.decode(enc), enc
            except Exception:
                continue
        return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

    # інші файли
    if data[:3] == b'raP':
        return rap_to_text(data), "rap-raw"
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

# ─── 6-7. SlotButton, SlotView, Claim система (без змін) ───────────────────────
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
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

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
            value=(sess["owners"][self.idx].mention
                   if sess["owners"][self.idx] else "Вільний"),
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

        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

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
        except:
            pass

        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            try:
                admin_msg = await admin_ch.fetch_message(self.admin_msg_id)
                await admin_msg.delete()
            except:
                pass

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

# ─── 8. ПОКРАЩЕНА команда імпорту mission.sqm / .pbo ───────────────────────────
@bot.command(name="імпорт_sqm")
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")

    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]
    
    await ctx.send("⏳ Обробка файлу...")

    try:
        text, method = await read_attachment_sqm_text(attachment)
    except Exception as e:
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    # Зберігаємо декодований текст для діагностики
    debug_path = f"/tmp/decoded_{attachment.filename}.txt"
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(text)
        await ctx.send(f"🔍 Декодований текст збережено: `{debug_path}` (метод: **{method}**)")
    except Exception:
        pass

    groups = parse_mission_sqm(text)
    
    if not groups:
        preview = "\n".join(text.splitlines()[:50])[:2000]
        await ctx.send(
            f"⚠️ Відділення у mission.sqm не знайдено.\n"
            f"Метод обробки: **{method}**\n"
            f"Розмір тексту: {len(text)} символів\n"
            f"Рядків: {len(text.splitlines())}\n\n"
            f"Прев'ю початку файлу:"
        )
        await ctx.send(f"```\n{preview}\n```")
        return

    target_ch = bot.get_channel(ADMIN_CHANNEL_ID)
    if not target_ch:
        return await ctx.send("❌ Адмін-канал не знайдено за ID.")

    sent_count = 0
    total_slots = 0
    
    for group_name, slot_list in groups:
        embed = build_group_embed(group_name, slot_list[:25])
        try:
            await target_ch.send(embed=embed)
            sent_count += 1
            total_slots += len(slot_list[:25])
        except Exception as e:
            await ctx.send(f"⚠️ Помилка відправки групи '{group_name}': {e}")

    await ctx.send(
        f"✅ Імпорт завершено!\n"
        f"📊 Знайдено відділень: **{sent_count}**\n"
        f"🎯 Всього слотів: **{total_slots}**\n"
        f"🔧 Метод обробки: **{method}**"
    )

# ─── 9. Події on_ready та on_message ────────────────────────────────────────────
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

# ─── 10. Сервісні команди ───────────────────────────────────────────────────────
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

# ─── 11. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)
