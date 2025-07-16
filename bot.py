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

# 1) Keep-alive та ENV
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# 2) Інтенти
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 3) Конфіг
KYIV_TZ         = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID  = 1160843618433630228
ADMIN_CHANNEL_ID= 1395065909185478769

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}         # message_id → {title, lines, owners, channel_id}
claims:   dict[tuple[int,int], list] = {}  # (msg_id, idx) → [User,...]

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# 4) VTG-нагадувач
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except Exception as e:
                print(f"[vtg_reminder] Error: {e}")

# 5) Генератор Embed
def build_embed(sess: dict) -> discord.Embed:
    e = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    e.description = "\n".join(lines)
    return e

# 6) Слоти: кнопки і View
class SlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        self.session_id = session_id
        self.idx = idx
        owner = sessions[session_id]["owners"][idx]
        free = owner is None
        super().__init__(
            label=f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}",
            style=discord.ButtonStyle.success if free else discord.ButtonStyle.danger,
            custom_id=f"slot-{session_id}-{idx}"
        )

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        ch_id = sess["channel_id"]

        # 6.1) Вільний слот → одразу зайняти, тільки 1 слот/гілка
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже займаєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.session_id))
            return

        # 6.2) Ваш слот → звільнити
        if owner == user:
            sess["owners"][self.idx] = None
            await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.session_id))
            return

        # 6.3) Зайнято іншим → пропозиція “Претендувати”
        view = ClaimSlotView(self.session_id, self.idx)
        await inter.response.send_message(
            f"⚠️ Слот уже зайнято {owner.mention}.", view=view, ephemeral=True
        )

class SlotView(View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[session_id]["lines"])):
            self.add_item(SlotButton(session_id, idx))

# 7) Кнопка “Претендувати”
class ClaimSlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        super().__init__(
            label="❗ Претендувати",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim-slot-{session_id}-{idx}"
        )
        self.session_id = session_id
        self.idx = idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        key = (self.session_id, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message("ℹ️ Ви вже подали заявку.", ephemeral=True)
        lst.append(user)
        await inter.response.send_message("✅ Заявка відправлена адміністрації.", ephemeral=True)

        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            embed = discord.Embed(title="📝 Нова заявка на слот", color=discord.Color.orange())
            embed.add_field(name="Сесія", value=sess["title"], inline=False)
            embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
            embed.add_field(name="Власник", value=(owner.mention if owner else "Ніхто"), inline=True)
            embed.add_field(name="Кандидат", value=user.mention, inline=False)
            await admin_ch.send(embed=embed, view=ClaimDecisionView(self.session_id, self.idx, user.id))

class ClaimSlotView(View):
    def __init__(self, session_id: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(session_id, idx))

# 8) Modal для причини
class DecisionModal(Modal):
    def __init__(self, session_id:int, idx:int, claimant_id:int, accept:bool):
        super().__init__(title="Причина призначення" if accept else "Причина відмови")
        self.session_id = session_id
        self.idx = idx
        self.claimant_id = claimant_id
        self.accept = accept
        self.reason = TextInput(label="Вкажіть причину", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.session_id]
        key  = (self.session_id, self.idx)
        claimant  = bot.get_user(self.claimant_id)
        old_owner = sess["owners"][self.idx]

        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)
            ch = bot.get_channel(sess["channel_id"])
            if ch:
                try:
                    msg = await ch.fetch_message(self.session_id)
                    await msg.edit(embed=build_embed(sess), view=SlotView(self.session_id))
                except: pass
            if old_owner and old_owner != claimant:
                try:
                    await old_owner.send(f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\nПричина: {self.reason.value}")
                except: pass
            try:
                await claimant.send(f"✅ Вас призначено на слот #{self.idx+1}.\nПричина: {self.reason.value}")
            except: pass
            await inter.response.send_message("✔️ Слот призначено.", ephemeral=True)
        else:
            lst = claims.get(key, [])
            if claimant in lst: lst.remove(claimant)
            try:
                await claimant.send(f"❌ Ваша заявка відхилена.\nПричина: {self.reason.value}")
            except: pass
            await inter.response.send_message("✖️ Заявку відхилено.", ephemeral=True)

# 9) View рішення для адміну
class ClaimDecisionButton(Button):
    def __init__(self, session_id:int, idx:int, claimant_id:int, accept:bool):
        super().__init__(
            label="✅ Призначити" if accept else "❌ Відхилити",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"dec-{'accept' if accept else 'deny'}-{session_id}-{idx}-{claimant_id}"
        )
        self.session_id = session_id
        self.idx = idx
        self.claimant_id = claimant_id
        self.accept = accept

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(
            DecisionModal(self.session_id, self.idx, self.claimant_id, self.accept)
        )

class ClaimDecisionView(View):
    def __init__(self, session_id:int, idx:int, claimant_id:int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, True))
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, False))

# 10) Події
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    # Restart-embed
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🔄 Бот перезапущено",
                        description=f"📦 Commit: `{commit}`",
                        color=discord.Color.green())
    for g in bot.guilds:
        ch = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages,
            g.text_channels
        )
        if ch:
            try: await ch.send(embed=emb)
            except: pass
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return

    lines = message.content.splitlines()
    if any("запис слоти" in l.lower() for l in lines):
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for raw in lines:
            txt = raw.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = None
                for m_ in message.mentions:
                    if f"<@{m_.id}>" in txt or f"<@!{m_.id}>" in txt:
                        owner = m_
                        break
                clean = MENTION_RE.sub("", txt).strip()
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

# 11) Сервісні команди
@bot.command()
async def статус(ctx):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit",   value=commit, inline=True)
    emb.add_field(name="Sessions", value=str(len(sessions)), inline=True)
    emb.add_field(name="Claims",   value=str(sum(len(v) for v in claims.values())), inline=True)
    await ctx.send(embed=emb)

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановлено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Деплой тригерено!")

@bot.command()
async def gitpush(ctx):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                         inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# 12) Старт
bot.run(TOKEN)