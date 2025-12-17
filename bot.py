import os
import re
import subprocess
import aiohttp
import datetime
import io
from zoneinfo import ZoneInfo

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
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))
# Якщо потрібно: ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))

# ─── 2. Інтенти та ініціалізація бота ───────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ          = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID   = 1160843618433630228

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}            # message_id → { title, lines, owners, channel_id }
claims: dict[tuple[int,int], list] = {}   # (message_id, idx) → [User, ...]
request_counter = 0                       # лічильник заявок

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Prikaati 'Karhu' | Jalkaväen haara"

# ─── [NEW] SQM PARSER LOGIC ─────────────────────────────────────────────────────
class SqmParser:
    def __init__(self):
        # 1. Паттерни для пошуку Командира (SL) різними мовами
        # Додано: vadītājs (лат), sergeant, komandieris
        self.sl_patterns = [
            r"командир", r"squad leader", r"komandir", r"nodalas komandieris", 
            r"sl", r"sql", r"officer", r"офіцер", r"lidem", r"vadītājs", r"sergeant"
        ]
        
        # 2. Паттерн для пошуку індексу групи (наприклад, "1-4", "2-1")
        self.index_pattern = re.compile(r"\b(\d+-\d+)\b")
        
        # 3. Паттерн сторони
        self.side_pattern = re.compile(r'side="?(\w+)"?;', re.IGNORECASE)

    def _normalize_slot(self, text):
        """
        Очищає текст слота: 
        - Залишає лише найдовшу зброю в дужках.
        - Прибирає нумерацію на початку.
        """
        # Знаходимо всі варіанти тексту в дужках (зброю)
        weapons = re.findall(r'\((.*?)\)', text)
        
        # Тимчасово видаляємо всі дужки з тексту, щоб очистити назву ролі
        text_no_weapons = re.sub(r'\(.*?\)', '', text)
        
        # Вибираємо найдовшу назву зброї (логіка: (G36A3\AG-40) довше ніж (G36))
        best_weapon = ""
        if weapons:
            best_weapon = max(weapons, key=len)
        
        # Чистимо текст від "1.", "2)", "|", зайвих пробілів
        text_clean = re.sub(r'^\s*\d+[\.\)]\s*', '', text_no_weapons) 
        text_clean = text_clean.replace('|', '').strip()
        text_clean = re.sub(r'\s+', ' ', text_clean) 
        
        # Збираємо фінальний рядок
        final_parts = [text_clean]
        if best_weapon:
            final_parts.append(f"({best_weapon})")
            
        return " ".join(final_parts)

    def _extract_group_index(self, text):
        """Шукає індекс типу 1-4 у тексті."""
        match = self.index_pattern.search(text)
        return match.group(1) if match else None

    def process_file(self, file_content_str):
        """
        Основний метод парсингу.
        Повертає відсортований список груп без дублікатів.
        """
        lines = file_content_str.splitlines()
        
        raw_units = [] 
        current_side = "UNKNOWN"
        
        # --- Етап 1: Лінійний прохід, збір всіх юнітів ---
        for line in lines:
            line = line.strip()
            
            # Зміна сторони
            side_match = self.side_pattern.search(line)
            if side_match:
                current_side = side_match.group(1).upper()
            
            # Пошук тексту слота
            if line.startswith('text=') or line.startswith('description='):
                match = re.search(r'"(.*)"', line)
                if match:
                    raw_text = match.group(1)
                    # Ігноруємо порожні або системні слоти
                    if raw_text and not raw_text.startswith("__"):
                        raw_units.append({
                            'side': current_side,
                            'text': raw_text
                        })

        # --- Етап 2: Формування відділень (Squads) ---
        groups_map = {} # Key: (Side, Index), Value: GroupDict
        
        current_squad_slots = []
        current_squad_side = None
        
        # Додаємо маркер кінця
        raw_units.append({'side': 'END', 'text': 'END_MARKER'})

        for unit in raw_units:
            text = unit['text']
            side = unit['side']
            
            # Перевіряємо, чи цей слот схожий на SL (початок нової групи)
            is_sl = any(re.search(pat, text, re.IGNORECASE) for pat in self.sl_patterns)
            
            # Умова розриву групи: SL знайден або зміна сторони
            should_close_group = (is_sl and current_squad_slots) or \
                                 (current_squad_side is not None and side != current_squad_side)

            if should_close_group:
                # Обробляємо накопичене відділення
                if current_squad_slots:
                    first_slot_raw = current_squad_slots[0]
                    
                    # --- ФІЛЬТР 1: Перший слот МАЄ БУТИ командиром ---
                    first_is_sl = any(re.search(pat, first_slot_raw, re.IGNORECASE) for pat in self.sl_patterns)
                    
                    if first_is_sl:
                        # Шукаємо індекс групи (наприклад "1-4")
                        g_idx = self._extract_group_index(first_slot_raw)
                        
                        # Формуємо чисту назву групи
                        name_buffer = first_slot_raw
                        for pat in self.sl_patterns:
                            name_buffer = re.sub(pat, "", name_buffer, flags=re.IGNORECASE)
                        
                        name_buffer = re.sub(r'\(.*?\)', '', name_buffer)
                        group_name = name_buffer.replace('|', '').strip().strip("-").strip()
                        
                        if not group_name:
                            group_name = f"Squad {g_idx}" if g_idx else "Infantry"
                        
                        full_title = f"[{current_squad_side}] {group_name}"

                        # Нормалізуємо слоти
                        formatted_slots = []
                        for i, raw_s in enumerate(current_squad_slots):
                            formatted_slots.append(f"{i+1}. {self._normalize_slot(raw_s)}")
                        
                        # --- ФІЛЬТР 2: Обробка дублікатів ---
                        # Якщо індексу немає, використовуємо хеш, щоб не втратити унікальні
                        if g_idx:
                            unique_key = (current_squad_side, g_idx)
                        else:
                            unique_key = (current_squad_side, f"NO_IDX_{len(groups_map)}")

                        new_group_data = {
                            'title': full_title,
                            'pure_name': group_name,
                            'slots': formatted_slots,
                            'side': current_squad_side,
                            'group_index': g_idx  # Зберігаємо для фільтрації в команді
                        }

                        # Логіка заміни дубліката: залишаємо ту назву, яка довша
                        if unique_key in groups_map:
                            existing = groups_map[unique_key]
                            if len(new_group_data['pure_name']) > len(existing['pure_name']):
                                groups_map[unique_key] = new_group_data
                        else:
                            groups_map[unique_key] = new_group_data

                current_squad_slots = []
            
            if text != 'END_MARKER':
                current_squad_slots.append(text)
                if len(current_squad_slots) == 1:
                    current_squad_side = side

        # Сортуємо
        sorted_groups = sorted(groups_map.values(), key=lambda x: (x['side'], x['title']))
        return sorted_groups

# Створюємо глобальний екземпляр
sqm_parser = SqmParser()

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

        # 6.1) Вільний слот → зайняти
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

        # 6.3) Чужий слот → пропонуємо претендувати
        return await inter.response.send_message(
            f"⚠️ Цей слот зайнято {owner.mention}.",
            view=ClaimSlotView(self.sid, self.idx),
            ephemeral=True
        )

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        if sid in sessions:
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

# ─── 8. Modal для рішення ───────────────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int, accept: bool):
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
                await claimant.send(f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
                if old_owner and old_owner != claimant:
                    await old_owner.send(f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\nПричина: {reason}")
            else:
                await claimant.send(f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\nПричина: {reason}")
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
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int, accept: bool):
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(
            label=label,
            style=style,
            custom_id=f"dec-{tag}-{sid}-{idx}-{claimant_id}-{admin_msg_id}"
        )
        self.sid, self.idx = sid, idx
        self.claimant_id, self.admin_msg_id, self.accept = claimant_id, admin_msg_id, accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(self.sid, self.idx, self.claimant_id, self.admin_msg_id, self.accept)
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, False))

# ─── 9. Зняття та запис (Admin) ────────────────────────────────────────────────
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
            except:
                pass

        try:
            await owner.send(f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
        except:
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
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(RemoveSlotButton(sid, idx))

@bot.command(name="зняти", aliases=["release"])
async def зняти(ctx: commands.Context, session_msg_id: int):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:", view=RemoveSlotView(session_msg_id))

class AssignSlotModal(Modal):
    def __init__(self, sid: int, idx: int, uid: int):
        super().__init__(title="Причина запису")
        self.sid, self.idx, self.uid = sid, idx, uid
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        user = await bot.fetch_user(self.uid)
        reason = self.reason.value

        if sess["owners"][self.idx] == user:
            return await inter.response.send_message(f"⚠️ {user.mention} вже записаний.", ephemeral=True)
        if sess["owners"][self.idx] is not None:
            return await inter.response.send_message(f"⚠️ Слот #{self.idx+1} зайнятий.", ephemeral=True)

        sess["owners"][self.idx] = user
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(self.sid)
                await msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass
        try:
            await user.send(f"✅ Вас записано на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
        except:
            pass
        await inter.response.send_message(f"📌 {user.mention} записано на слот #{self.idx+1}.", ephemeral=True)

class AssignSlotButton(Button):
    def __init__(self, sid: int, idx: int, uid: int):
        super().__init__(label=str(idx+1), style=discord.ButtonStyle.success, custom_id=f"assign-{sid}-{idx}-{uid}")
        self.sid, self.idx, self.uid = sid, idx, uid

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(AssignSlotModal(self.sid, self.idx, self.uid))

class AssignSlotView(View):
    def __init__(self, sid: int, uid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(AssignSlotButton(sid, idx, uid))

@bot.command(name="записати")
async def записати(ctx: commands.Context, session_msg_id: int, member: discord.Member):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(f"📋 Оберіть слот для запису {member.mention} в сесії {session_msg_id}:", view=AssignSlotView(session_msg_id, member.id))

# ─── 10. Події on_ready та on_message ────────────────────────────────────────────
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
        sess = {
            "title": header or DEFAULT_TITLE,
            "lines": slots,
            "owners": owners,
            "channel_id": message.channel.id
        }
        embed = build_embed(sess)
        sent = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── 11. Сервісні команди та Імпорт ─────────────────────────────────────────────
@bot.command(name='import_sqm')
async def import_sqm(ctx, filter_idx: str = None):
    """
    Імпорт слотів з файлу mission.sqm.
    Можна вказати фільтр: !import_sqm 1-4
    """
    if not ctx.message.attachments:
        await ctx.send("❌ Будь ласка, прикріпіть файл `mission.sqm` до повідомлення.")
        return

    attachment = ctx.message.attachments[0]
    valid_ext = ('.sqm', '.txt', '.cpp')
    if not attachment.filename.lower().endswith(valid_ext):
        await ctx.send("❌ Файл повинен мати розширення .sqm (або .txt).")
        return

    status_msg = await ctx.send("⏳ Завантаження та обробка файлу...")

    try:
        file_bytes = await attachment.read()
        content = file_bytes.decode('utf-8', errors='ignore')

        # Запускаємо парсер
        groups = sqm_parser.process_file(content)

        # Логіка фільтрації, якщо вказано аргумент
        if filter_idx:
            filtered_groups = []
            filter_lower = filter_idx.lower()
            for g in groups:
                # Перевіряємо точний збіг індексу АБО входження в назву
                match_index = g.get('group_index') and g['group_index'] == filter_idx
                match_title = filter_lower in g['title'].lower()
                
                if match_index or match_title:
                    filtered_groups.append(g)
            
            if not filtered_groups:
                await status_msg.edit(content=f"❌ За запитом `{filter_idx}` відділень не знайдено.")
                return
            groups = filtered_groups

        if not groups:
            await status_msg.edit(content="⚠️ Відділень не знайдено.")
            return

        await status_msg.edit(content=f"✅ Знайдено відділень: {len(groups)}. Генерую списки...")

        current_message_content = ""
        
        for group in groups:
            header = f"**{group['title']}**"
            slots_block = "\n".join(group['slots'])
            full_block = f"{header}\n```\n{slots_block}\n```\n"
            
            if len(current_message_content) + len(full_block) > 1900:
                await ctx.send(current_message_content)
                current_message_content = ""
            
            current_message_content += full_block

        if current_message_content:
            await ctx.send(current_message_content)
            
        await ctx.send("🏁 Імпорт завершено!")

    except Exception as e:
        print(f"Error parsing SQM: {e}")
        await ctx.send(f"❌ Помилка: {e}")

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
    emb.add_field(name="2. git add", value="`git add .`", inline=False)
    emb.add_field(name="3. git commit", value='`git commit -m "Оновлення"`', inline=False)
    emb.add_field(name="4. git push", value="`git push origin main`", inline=False)
    await ctx.send(embed=emb)

# ─── 12. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)
