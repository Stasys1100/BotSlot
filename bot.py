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
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID    = 1160843618433630228
ADMIN_CHANNEL_ID  = 1395065909185478769

# для унікальності обробки “запис слоти”
processed_messages: set[int] = set()

# сесії слотів: message_id → { title, lines, owners, channel_id }
sessions: dict[int, dict] = {}

# заявки на зайняті слоти: (embed_msg_id, slot_index) → [User, ...]
claims: dict[tuple[int, int], list[discord.User]] = {}

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
            except Exception as e:
                print(f"[vtg_reminder] Error: {e}")


# ─── 5. Віджет Embed для сесії ───────────────────────────────────────────────────
def build_embed(sess: dict) -> discord.Embed:
    e = discord.Embed(title=sess["title"], color=discord.Color.blue())
    desc = []
    for i, (line, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        if owner:
            desc.append(f"{i+1}. {line}  –  Зайнято {owner.mention}")
        else:
            desc.append(f"{i+1}. {line}")
    e.description = "\n".join(desc)
    return e


# ─── 6. Кнопки та View для слотів ──────────────────────────────────────────────
class SlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        self.session_id = session_id
        self.idx = idx
        owner = sessions[session_id]["owners"][idx]
        free = owner is None
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{session_id}-{idx}")

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        channel_id = sess["channel_id"]

        # 1) Вільний слот → негайно зайняти, але перевірити, що в гілці нема інших
        if owner is None:
            for other in sessions.values():
                if other["channel_id"] == channel_id and user in other["owners"]:
                    return await interaction.response.send_message(
                        "⚠️ Ви вже займаєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            await interaction.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.session_id)
            )
            return

        # 2) Якщо це ваш слот → звільнити
        if owner == user:
            sess["owners"][self.idx] = None
            await interaction.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.session_id)
            )
            return

        # 3) Чужий слот → показати кнопку “Претендувати”
        view = ClaimSlotView(self.session_id, self.idx)
        await interaction.response.send_message(
            f"⚠️ Цей слот зайнято {owner.mention}.", view=view, ephemeral=True
        )


class SlotView(View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[session_id]["lines"])):
            self.add_item(SlotButton(session_id, idx))


# ─── 7. “Претендувати” на слот ─────────────────────────────────────────────────
class ClaimSlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        self.session_id = session_id
        self.idx = idx
        super().__init__(
            label="❗ Претендувати",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim-slot-{session_id}-{idx}"
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        key = (self.session_id, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await interaction.response.send_message(
                "ℹ️ Ви вже подали заявку.", ephemeral=True
            )
        lst.append(user)
        await interaction.response.send_message(
            "✅ Заявка відправлена адміністрації.", ephemeral=True
        )

        # нотифікація адміну
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            embed = discord.Embed(
                title="📝 Новий кандидат на слот",
                color=discord.Color.orange()
            )
            embed.add_field(name="Сесія", value=sess["title"], inline=False)
            embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
            embed.add_field(name="Власник", value=(owner.mention if owner else "Ніхто"), inline=True)
            embed.add_field(name="Кандидат", value=user.mention, inline=False)
            view = ClaimDecisionView(self.session_id, self.idx, user.id)
            await admin_ch.send(embed=embed, view=view)


class ClaimSlotView(View):
    def __init__(self, session_id: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(session_id, idx))


# ─── 8. Модал для вводу причини ─────────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(self, session_id: int, idx: int, claimant_id: int, accept: bool):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.session_id = session_id
        self.idx = idx
        self.claimant_id = claimant_id
        self.accept = accept

        self.reason = TextInput(
            label="Вкажіть причину",
            style=discord.TextStyle.paragraph,
            placeholder="Напишіть деталі..."
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        sess = sessions[self.session_id]
        key  = (self.session_id, self.idx)
        claimant = bot.get_user(self.claimant_id)
        owner   = sess["owners"][self.idx]

        # якщо затверджено — перепризначимо
        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)

            # оновити оригінальний меседж
            orig_ch = bot.get_channel(sess["channel_id"])
            if orig_ch:
                try:
                    orig = await orig_ch.fetch_message(self.session_id)
                    await orig.edit(embed=build_embed(sess), view=SlotView(self.session_id))
                except: pass

            # повідомити попереднього власника
            if owner and owner != claimant:
                try:
                    await owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} у «{sess['title']}» передано {claimant.mention}.\n"
                        f"Причина: {self.reason.value}"
                    )
                except: pass

            # повідомити нового власника
            try:
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\n"
                    f"Причина: {self.reason.value}"
                )
            except: pass

            await interaction.response.send_message("✔️ Слот призначено.", ephemeral=True)
        else:
            # відхилити кандидата
            lst = claims.get(key, [])
            if claimant in lst:
                lst.remove(claimant)
            try:
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\n"
                    f"Причина: {self.reason.value}"
                )
            except: pass
            await interaction.response.send_message("✖️ Заявку відхилено.", ephemeral=True)


# ─── 9. Кнопки admin: Призначити / Відхилити ────────────────────────────────────
class ClaimDecisionButton(Button):
    def __init__(self, session_id: int, idx: int, claimant_id: int, accept: bool):
        self.session_id = session_id
        self.idx = idx
        self.claimant_id = claimant_id
        self.accept = accept
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(label=label, style=style, custom_id=f"dec-{tag}-{session_id}-{idx}-{claimant_id}")

    async def callback(self, interaction: discord.Interaction):
        # на натискання відкриваємо модал для вводу причини
        modal = DecisionModal(self.session_id, self.idx, self.claimant_id, self.accept)
        await interaction.response.send_modal(modal)


class ClaimDecisionView(View):
    def __init__(self, session_id: int, idx: int, claimant_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, accept=True))
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, accept=False))


# ─── 10. Події ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] Logged in as {bot.user}")
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # уникаємо дублювання при редагуванні/повторі
    if message.id in processed_messages:
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
                for mention in message.mentions:
                    if f"<@{mention.id}>" in txt or f"<@!{mention.id}>" in txt:
                        owner = mention
                        break
                clean = MENTION_RE.sub("", txt).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = txt

        slots = slots[:25]
        owners = owners[:len(slots)]
        session = {
            "title":      header or DEFAULT_TITLE,
            "lines":      slots,
            "owners":     owners,
            "channel_id": message.channel.id
        }

        embed = build_embed(session)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = session
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)


# ─── 11. Команди сервісу ───────────────────────────────────────────────────────
@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Sessions", value=str(len(sessions)), inline=True)
    emb.add_field(name="Claims", value=str(sum(len(v) for v in claims.values())), inline=True)
    await ctx.send(embed=emb)

@bot.command()
async def оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановлено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Деплой тригерено!")

@bot.command()
async def gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                         inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)


# ─── 12. Запуск бота ───────────────────────────────────────────────────────────
bot.run(TOKEN)