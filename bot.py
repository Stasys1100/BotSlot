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
# ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID")) # Якщо потрібно

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

# ─── 4. SQM PARSER (Нова логіка) ────────────────────────────────────────────────
class SqmParser:
    def __init__(self):
        # Паттерни для пошуку Командира
        self.sl_patterns = [
            r"командир", r"squad leader", r"komandir", r"nodalas komandieris", 
            r"sl", r"sql", r"officer", r"офіцер", r"lidem", r"vadītājs", r"sergeant",
            r"leader", r"ст\.", r"старший"
        ]
        self.index_pattern = re.compile(r"\b(\d+-\d+)\b")
        self.side_pattern = re.compile(r'side="?(\w+)"?;', re.IGNORECASE)

    def _clean_role_name(self, text):
        """Очищає назву ролі від сміття, зброї та частини після @"""
        # Прибираємо зброю в дужках тимчасово
        text = re.sub(r'\(.*?\)', '', text)
        # Прибираємо нумерацію на початку (1. 2.)
        text = re.sub(r'^\s*\d+[\.\)]\s*', '', text)
        # Якщо є @, беремо тільки ліву частину (Роль)
        if "@" in text:
            text = text.split("@")[0]
        return text.strip()

    def _normalize_slot(self, text):
        """Формує красивий рядок: Роль (Зброя)"""
        # Пошук найдовшої назви зброї в дужках
        weapons = re.findall(r'\((.*?)\)', text)
        best_weapon = max(weapons, key=len) if weapons else ""

        role_clean = self._clean_role_name(text)
        
        final_parts = [role_clean]
        if best_weapon:
            final_parts.append(f"({best_weapon})")
            
        return " ".join(final_parts)

    def _extract_group_index(self, text):
        match = self.index_pattern.search(text)
        return match.group(1) if match else None

    def process_file(self, file_content_str):
        lines = file_content_str.splitlines()
        raw_units = [] 
        current_side = "UNKNOWN"
        
        # Етап 1: Лінійний збір
        for line in lines:
            line = line.strip()
            side_match = self.side_pattern.search(line)
            if side_match:
                current_side = side_match.group(1).upper()
            
            if line.startswith('text=') or line.startswith('description='):
                match = re.search(r'"(.*)"', line)
                if match:
                    raw_text = match.group(1)
                    if raw_text and not raw_text.startswith("__"):
                        raw_units.append({'side': current_side, 'text': raw_text})

        # Етап 2: Формування відділень
        groups_map = {} 
        current_squad_slots = []
        current_squad_side = None
        
        raw_units.append({'side': 'END', 'text': 'END_MARKER'})

        for unit in raw_units:
            text = unit['text']
            side = unit['side']
            
            # Перевірка на командира
            is_sl = any(re.search(pat, text, re.IGNORECASE) for pat in self.sl_patterns)
            
            # Закриваємо групу якщо новий SL або зміна сторони
            should_close_group = (is_sl and current_squad_slots) or \
                                 (current_squad_side is not None and side != current_squad_side)

            if should_close_group:
                if current_squad_slots:
                    first_slot_raw = current_squad_slots[0]
                    # Перевіряємо ще раз перший слот
                    if any(re.search(pat, first_slot_raw, re.IGNORECASE) for pat in self.sl_patterns):
                        
                        g_idx = self._extract_group_index(first_slot_raw)
                        group_name = ""

                        # Логіка з @ (Роль@НазваГрупи)
                        if "@" in first_slot_raw:
                            parts = first_slot_raw.split("@", 1)
                            group_name = parts[1].strip()
                            group_name = re.sub(r'\(.*?\)', '', group_name).strip()
                        else:
                            name_buffer = first_slot_raw
                            for pat in self.sl_patterns:
                                name_buffer = re.sub(pat, "", name_buffer, flags=re.IGNORECASE)
                            group_name = re.sub(r'\(.*?\)', '', name_buffer).replace('|', '').strip().strip("-").strip()

                        if not group_name:
                            group_name = f"Squad {g_idx}" if g_idx else "Infantry"
                        
                        full_title = f"[{current_squad_side}] {group_name}"

                        formatted_slots = []
                        for i, raw_s in enumerate(current_squad_slots):
                            formatted_slots.append(f"{i+1}. {self._normalize_slot(raw_s)}")
                        
                        # Унікальний ключ
                        if g_idx:
                            unique_key = (current_squad_side, g_idx)
                        else:
                            unique_key = (current_squad_side, f"NO_IDX_{len(groups_map)}_{group_name}")

                        new_group_data = {
                            'title': full_title,
                            'pure_name': group_name,
                            'slots': formatted_slots,
                            'side': current_squad_side,
                            'group_index': g_idx
                        }

                        # Фільтр дублікатів (залишаємо довшу назву)
                        if unique_key in groups_map:
                            if len(new_group_data['pure_name']) > len(groups_map[unique_key]['pure_name']):
                                groups_map[unique_key] = new_group_data
                        else:
                            groups_map[unique_key] = new_group_data

                current_squad_slots = []
            
            if text != 'END_MARKER':
                current_squad_slots.append(text)
                if len(current_squad_slots) == 1:
                    current_squad_side = side

        return sorted(groups_map.values(), key=lambda x: (x['side'], x['title']))

sqm_parser = SqmParser()
# ─── 5. Щотижневий нагадувач VTG ────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try: await ch.send("||@everyone||\n**Сбор VTG**")
            except: pass

# ─── 6. Генератор Embed ────────────────────────────────────────────────────────
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

# ─── 7. SlotButton та SlotView ─────────────────────────────────────────────────
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

        # 1) Вільний слот
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message("⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True)
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))

        # 2) Свій слот
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))

        # 3) Чужий слот
        return await inter.response.send_message(f"⚠️ Цей слот зайнято {owner.mention}.", view=ClaimSlotView(self.sid, self.idx), ephemeral=True)

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(SlotButton(sid, idx))

# ─── 8. Система "Претендувати" ─────────────────────────────────────────────────
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
        # ─── 9. Modal та Рішення по заявках ─────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int, accept: bool):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.sid, self.idx, self.claimant_id, self.admin_msg_id, self.accept = sid, idx, claimant_id, admin_msg_id, accept
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

        # Оновлення головного повідомлення
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass

        # DM повідомлення
        try:
            if self.accept:
                await claimant.send(f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
                if old_owner and old_owner != claimant:
                    await old_owner.send(f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\nПричина: {reason}")
            else:
                await claimant.send(f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\nПричина: {reason}")
        except: pass

        # Видалення адмін-запиту
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
        super().__init__(label=label, style=style, custom_id=f"dec-{'acc' if accept else 'dny'}-{sid}-{idx}-{claimant_id}")
        self.sid, self.idx, self.claimant_id, self.admin_msg_id, self.accept = sid, idx, claimant_id, admin_msg_id, accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(self.sid, self.idx, self.claimant_id, self.admin_msg_id, self.accept)
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(self, sid: int, idx: int, claimant_id: int, admin_msg_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, False))

# ─── 10. Адмін-інструменти: ЗНЯТТЯ ──────────────────────────────────────────────
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
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(RemoveSlotButton(sid, idx))

# ─── 11. Адмін-інструменти: ЗАПИС ───────────────────────────────────────────────
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

        if sess["owners"][self.idx] is not None:
            return await inter.response.send_message(f"⚠️ Слот #{self.idx+1} вже зайнятий.", ephemeral=True)

        sess["owners"][self.idx] = user
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(self.sid)
                await msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass

        try:
            await user.send(f"✅ Вас записано на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}")
        except: pass

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
                # ─── 12. Події on_ready та on_message ───────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(title="🔄 Бот перезапущено", description=f"📦 Commit: `{commit}`", color=discord.Color.green())
    for guild in bot.guilds:
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages, guild.text_channels)
        if ch:
            try: await ch.send(embed=embed)
            except: pass
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return

    # Стара логіка (ручний запис слотів через повідомлення)
    if "запис слоти" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower(): continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = next((u for u in message.mentions if f"<@{u.id}>" in txt or f"<@!{u.id}>" in txt), None)
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None: header = txt

        slots, owners = slots[:25], owners[:len(slots)]
        sess = {"title": header or DEFAULT_TITLE, "lines": slots, "owners": owners, "channel_id": message.channel.id}
        embed = build_embed(sess)
        sent = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── 13. Команди бота ───────────────────────────────────────────────────────────
@bot.command(name='import_sqm')
async def import_sqm(ctx, filter_idx: str = None):
    """
    Імпорт mission.sqm.
    Використання: !import_sqm (всі) АБО !import_sqm 1-4 (фільтр)
    """
    if not ctx.message.attachments:
        return await ctx.send("❌ Будь ласка, прикріпіть файл `mission.sqm`.")

    attachment = ctx.message.attachments[0]
    # Перевірка розширення
    if not any(attachment.filename.lower().endswith(ext) for ext in ['.sqm', '.txt', '.cpp']):
        return await ctx.send("❌ Файл повинен бути .sqm, .txt або .cpp.")

    status_msg = await ctx.send("⏳ Обробка файлу...")

    try:
        file_bytes = await attachment.read()
        content = file_bytes.decode('utf-8', errors='ignore')

        # Запуск парсера
        groups = sqm_parser.process_file(content)

        # Фільтрація
        if filter_idx:
            filtered_groups = []
            filter_lower = filter_idx.lower()
            for g in groups:
                # Збіг індексу або входження в назву
                match_idx = g.get('group_index') and g['group_index'] == filter_idx
                match_title = filter_lower in g['title'].lower()
                
                if match_idx or match_title:
                    filtered_groups.append(g)
            
            if not filtered_groups:
                return await status_msg.edit(content=f"❌ За запитом `{filter_idx}` нічого не знайдено.")
            groups = filtered_groups

        if not groups:
            return await status_msg.edit(content="⚠️ Відділень не знайдено.")

        await status_msg.edit(content=f"✅ Знайдено відділень: {len(groups)}.")

        # Вивід (Batching)
        current_message = ""
        for group in groups:
            # Жирний заголовок + слоти в блоці коду
            block = f"**{group['title']}**\n```\n" + "\n".join(group['slots']) + "\n```\n"
            
            if len(current_message) + len(block) > 1900:
                await ctx.send(current_message)
                current_message = ""
            current_message += block

        if current_message:
            await ctx.send(current_message)
        await ctx.send("🏁 Імпорт завершено!")

    except Exception as e:
        await ctx.send(f"❌ Помилка: {e}")

@bot.command(name="зняти", aliases=["release"])
async def зняти(ctx: commands.Context, session_msg_id: int):
    if ctx.channel.id != ADMIN_CHANNEL_ID: return
    session = sessions.get(session_msg_id)
    if not session: return await ctx.send("❌ Сесія не знайдена.")
    await ctx.send(f"📋 Оберіть слот для звільнення (ID: {session_msg_id}):", view=RemoveSlotView(session_msg_id))

@bot.command(name="записати")
async def записати(ctx: commands.Context, session_msg_id: int, member: discord.Member):
    if ctx.channel.id != ADMIN_CHANNEL_ID: return
    session = sessions.get(session_msg_id)
    if not session: return await ctx.send("❌ Сесія не знайдена.")
    await ctx.send(f"📋 Оберіть слот для запису {member.mention} (ID: {session_msg_id}):", view=AssignSlotView(session_msg_id, member.id))

@bot.command(name="оновити", aliases=["update"])
async def _оновити(ctx: commands.Context):
    if DEPLOY_HOOK_URL:
        async with aiohttp.ClientSession() as sess: await sess.post(DEPLOY_HOOK_URL)
        await ctx.send("🔄 Деплой тригерено!")
    else:
        await ctx.send("❌ DEPLOY_HOOK_URL не налаштовано.")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

# ─── 14. Запуск ─────────────────────────────────────────────────────────────────
bot.run(TOKEN)
