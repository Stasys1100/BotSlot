import os
import re
import subprocess
import aiohttp
import datetime
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

# ─── 2. Інтенти та ініціалізація бота ───────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ          = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID   = 1160843618433630228
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))

processed_messages: set[int] = set()
# sessions: message_id → { title, lines, owners, channel_id, forbidden }
sessions: dict[int, dict] = {}            
claims: dict[tuple[int,int], list] = {}   # (message_id, idx) → [User, ...]
request_counter = 0                       # лічильник заявок

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "3. Prikaati 'Karhu' | Jalkaväen haara"

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
        owner = sessions.get(sid, {}).get("owners", [None])[idx]
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

        # ПЕРЕВІРКА НА ЗАБОРОНУ (для звичайних користувачів)
        forbidden_ids = sess.get("forbidden", [])[self.idx]
        if user.id in forbidden_ids:
            return await inter.response.send_message(
                "⛔ Цей слот заборонено для вас.", ephemeral=True
            )

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

        # ПЕРЕВІРКА НА ЗАБОРОНУ (для звичайних користувачів)
        forbidden_ids = sess.get("forbidden", [])[self.idx]
        if user.id in forbidden_ids:
            return await inter.response.send_message(
                "⛔ Ви не можете претендувати на цей слот (заборонено).", ephemeral=True
            )

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

        # Оновлюємо головне повідомлення
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        # DM користувачам
        try:
            if self.accept:
                # ЗМІНА: Додано ID сесії, причину не відправляємо призначеному
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid})."
                )
                if old_owner and old_owner != claimant:
                    # ЗМІНА: Додано ID сесії, причину відправляємо знятому
                    await old_owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention} у «{sess['title']}» (ID: {self.sid}).\n"
                        f"Причина: {reason}"
                    )
            else:
                # ЗМІНА: Додано ID сесії
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid}) відхилена.\n"
                    f"Причина: {reason}"
                )
        except:
            pass

        # Видаляємо адмін-повідомлення
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

# ─── 9. Зняття через кнопки та Modal ───────────────────────────────────────────
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

        sess["owners"][self.idx] = None
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        try:
            # ЗМІНА: Додано ID сесії
            await owner.send(
                f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}» (ID: {self.sid}).\n"
                f"Причина: {reason}"
            )
        except:
            pass

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
    await ctx.send(
        f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:",
        view=RemoveSlotView(session_msg_id)
    )

# ─── 9.5. Команда !записати ─────────────────────────────────────────────────────
@bot.command(name="записати")
async def записати(ctx: commands.Context, session_msg_id: int, member: discord.Member):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(
        f"📋 Оберіть слот для запису {member.mention} в сесії {session_msg_id}:",
        view=AssignSlotView(session_msg_id, member.id)
    )

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
            return await inter.response.send_message(
                f"⚠️ {user.mention} вже записаний на слот #{self.idx+1}.", ephemeral=True
            )
        if sess["owners"][self.idx] is not None:
            return await inter.response.send_message(
                f"⚠️ Слот #{self.idx+1} вже зайнятий {sess['owners'][self.idx].mention}.", 
                ephemeral=True
            )

        sess["owners"][self.idx] = user
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(self.sid)
                await msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        try:
            # ЗМІНА: Додано ID сесії, причину не відправляємо
            await user.send(
                f"✅ Вас записано на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid})."
            )
        except:
            pass

        await inter.response.send_message(
            f"📌 {user.mention} записано на слот #{self.idx+1}.", ephemeral=True
        )

class AssignSlotButton(Button):
    def __init__(self, sid: int, idx: int, uid: int):
        super().__init__(
            label=str(idx+1),
            style=discord.ButtonStyle.success,
            custom_id=f"assign-{sid}-{idx}-{uid}"
        )
        self.sid, self.idx, self.uid = sid, idx, uid

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(AssignSlotModal(self.sid, self.idx, self.uid))

class AssignSlotView(View):
    def __init__(self, sid: int, uid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(AssignSlotButton(sid, idx, uid))

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
        header = None
        slots = []
        owners = []
        forbidden_matrix = [] # Список списків ID (per slot)
        
        # Регулярний вираз для пошуку "заборонити @люди" (case-insensitive)
        FORBIDDEN_CLEAN_RE = re.compile(r'\s*заборонити\s*(\s*(?:<@!?(?P<id>\d+)>|\s|,|[^,>])+\s*)$', re.I)

        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                raw_content = m.group(2)
                
                line_owner = None
                line_forbidden = []
                final_text = raw_content
                
                # 1. Парсинг заборони та ВИДАЛЕННЯ тексту
                match_forbidden = FORBIDDEN_CLEAN_RE.search(raw_content)
                
                if match_forbidden:
                    # 1.1. Витягуємо список заборонених ID з знайденої частини
                    forbidden_part = match_forbidden.group(1)
                    for id_match in MENTION_RE.finditer(forbidden_part):
                        line_forbidden.append(int(id_match.group('id')))
                        
                    # 1.2. Видаляємо частину з "заборонити" з тексту слота
                    final_text = raw_content[:match_forbidden.start()]
                
                # 2. Визначення власника (шукаємо згадку в оригінальному *raw_content*)
                
                # Визначаємо, чи є в слоті згадка користувача, який НЕ є в списку заборонених
                potential_owner_mentions = [
                    u for u in message.mentions 
                    if u.id not in line_forbidden
                    and (f"<@{u.id}>" in raw_content or f"<@!{u.id}>" in raw_content)
                ]
                
                # Якщо є явна згадка користувача, який не в списку заборон, робимо його власником.
                if potential_owner_mentions:
                    line_owner = potential_owner_mentions[0]

                # 3. Видалення ЗГАДКИ власника (якщо його знайдено)
                if line_owner:
                    # Видаляємо згадку власника з *вже очищеного* від "заборонити" тексту
                    final_text = re.sub(fr'<@!?{line_owner.id}>', '', final_text)

                # 4. Фінальна зачистка від зайвих пробілів/ком
                final_text = final_text.strip()
                final_text = re.sub(r'\s{2,}', ' ', final_text) 
                final_text = re.sub(r'[\s,.:;]+$', '', final_text)

                slots.append(final_text)
                owners.append(line_owner) 
                forbidden_matrix.append(line_forbidden)

            elif header is None:
                header = txt

        # Обрізаємо до 25 (ліміт Embed field/rows)
        slots = slots[:25]
        owners = owners[:len(slots)]
        forbidden_matrix = forbidden_matrix[:len(slots)]

        sess = {
            "title":      header or DEFAULT_TITLE,
            "lines":      slots,
            "owners":     owners,
            "channel_id": message.channel.id,
            "forbidden":  forbidden_matrix  # зберігаємо список заборонених
        }
        embed = build_embed(sess)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

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

# ─── 12. Команда !слоти — парсинг SQM файлу місії ──────────────────────────────

def parse_sqm_slots(content: str) -> dict[str, dict]:
    """
    Парсить .sqm файл місії Arma 3 та повертає словник відділень.
    Ключ: назва відділення (напр. "Альфа 1-2")
    Значення: {"title": "Альфа 1-2 || Мотопіхотне відділення ІІ Буран", "slots": [...], "side": "blufor/opfor/indep"}
    """
    import re

    # Витягуємо всі description та їх сусідній контекст (side)
    # Шукаємо блоки: description + side поряд у тому самому unit-блоці
    # Простий підхід: зібрати всі description по порядку
    descriptions = re.findall(r'description="([^"]*)"', content)

    groups: dict[str, dict] = {}
    current_group: str | None = None
    current_title: str | None = None
    current_slots: list[str] = []

    for desc in descriptions:
        if '@' in desc:
            # Зберігаємо попереднє відділення
            if current_group and current_slots:
                if current_group not in groups:
                    groups[current_group] = {"title": current_title, "slots": current_slots[:]}
                # Якщо вже є — значить дублікат (обидві сторони), пропускаємо

            # Парсимо новий заголовок
            # Формат: "N. Роль @Назва X-Y  ІІ  Тип відділення ..."
            m = re.search(r'@(\S+\s+[\d\-]+)\s*(?:ІІ|II)\s*(.*)', desc)
            if m:
                current_group = m.group(1).strip()
                type_name = re.sub(r'\s{2,}', ' ', m.group(2).strip())
                current_title = f"{current_group} || {type_name}"
                # Роль першого слоту — до знаку @
                role_m = re.match(r'\d+\.\s*(.+?)\s*@', desc)
                first_role = role_m.group(1).strip() if role_m else "Слот 1"
                current_slots = [first_role]
            else:
                # Не вдалося розпарсити — скидаємо
                current_group = None
                current_slots = []
                current_title = None
        elif current_group:
            slot_m = re.match(r'\d+\.\s*(.*)', desc)
            if slot_m:
                current_slots.append(slot_m.group(1).strip())

    # Зберігаємо останнє відділення
    if current_group and current_slots and current_group not in groups:
        groups[current_group] = {"title": current_title, "slots": current_slots[:]}

    return groups


# Глобальний кеш розпарсених відділень: guild_id → {group_name → data}
mission_cache: dict[int, dict[str, dict]] = {}


@bot.command(name="слоти")
async def слоти(ctx: commands.Context, group_id: str = None, side: str = None):
    """
    Використання:
      !слоти <назва-відділення> [сторона]   — показати слоти відділення
      !слоти список                          — список всіх відділень
      (з прикріпленим .sqm файлом)          — завантажити місію
    
    Приклади:
      !слоти 1-2 blufor
      !слоти список
    """
    guild_id = ctx.guild.id if ctx.guild else ctx.author.id

    # ── Якщо є прикріплений файл — завантажуємо місію ──
    if ctx.message.attachments:
        att = ctx.message.attachments[0]
        if not att.filename.endswith(".sqm"):
            return await ctx.send("❌ Потрібен файл з розширенням `.sqm`.")
        
        await ctx.send("⏳ Зчитую файл місії...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(att.url) as resp:
                    raw = await resp.read()
            # Пробуємо декодувати (UTF-8 або Windows-1251)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("windows-1251", errors="replace")

            groups = parse_sqm_slots(content)
            if not groups:
                return await ctx.send("❌ Не знайдено жодного відділення у файлі місії.")

            mission_cache[guild_id] = groups
            await ctx.send(
                f"✅ Місію завантажено! Знайдено **{len(groups)}** відділень.\n"
                f"Використовуй `!слоти список` щоб побачити всі, або `!слоти 1-2 blufor` для конкретного."
            )
        except Exception as e:
            await ctx.send(f"❌ Помилка читання файлу: `{e}`")
        return

    # ── Якщо немає кешу — просимо завантажити ──
    if guild_id not in mission_cache:
        return await ctx.send(
            "❌ Місія не завантажена. Використай команду `!слоти` з прикріпленим `.sqm` файлом."
        )

    groups = mission_cache[guild_id]

    # ── !слоти список ──
    if group_id is None or group_id.lower() == "список":
        lines = []
        for name, data in groups.items():
            lines.append(f"• `{name}` — {data['title']}")
        
        # Розбиваємо на частини якщо забагато
        chunk = []
        chunk_len = 0
        for line in lines:
            if chunk_len + len(line) > 1800:
                embed = discord.Embed(
                    title="📋 Відділення місії",
                    description="\n".join(chunk),
                    color=discord.Color.blue()
                )
                await ctx.send(embed=embed)
                chunk = []
                chunk_len = 0
            chunk.append(line)
            chunk_len += len(line) + 1

        if chunk:
            embed = discord.Embed(
                title="📋 Відділення місії",
                description="\n".join(chunk),
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)
        return

    # ── !слоти <ID> [side] ──
    # Нормалізуємо пошук: шукаємо по частині назви
    query = group_id.strip().lower()
    side_query = (side or "").lower()

    # Збираємо всі підходящі відділення
    matches = []
    for name, data in groups.items():
        name_lower = name.lower()
        # Шукаємо по номеру відділення (напр "1-2" входить в "Альфа 1-2")
        if query in name_lower:
            matches.append((name, data))

    if not matches:
        return await ctx.send(
            f"❌ Відділення `{group_id}` не знайдено. "
            f"Використай `!слоти список` щоб побачити всі доступні."
        )

    # Якщо вказана сторона — намагаємось відфільтрувати за типом відділення в назві
    # В SQM обидві сторони мають однакові назви відділень, але різні типи
    # Blufor = рейнджери/США; Opfor = ПВК/Буран; Indep = незалежні
    SIDE_KEYWORDS = {
        "blufor": ["рейнджер", "снайпер", "мінометн", "медичн", "apache", "chinook", "soar", "armія сша"],
        "opfor":  ["пвк", "буран", "бтр", "land cruiser", "зу-23"],
        "indep":  [],
    }

    if side_query and side_query in SIDE_KEYWORDS:
        keywords = SIDE_KEYWORDS[side_query]
        filtered = []
        for name, data in matches:
            title_lower = data["title"].lower()
            if any(kw in title_lower for kw in keywords):
                filtered.append((name, data))
        if filtered:
            matches = filtered

    # Якщо кілька збігів — показуємо всі
    for name, data in matches:
        slots_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(data["slots"]))
        embed = discord.Embed(
            title=data["title"],
            description=slots_text,
            color=discord.Color.green() if "рейнджер" in data["title"].lower() or "soar" in data["title"].lower()
                  else discord.Color.red() if any(k in data["title"].lower() for k in ["пвк","буран","бтр"])
                  else discord.Color.orange()
        )
        embed.set_footer(text=f"Слотів: {len(data['slots'])}")
        await ctx.send(embed=embed)


# ─── 13. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)
