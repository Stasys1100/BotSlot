import os
import re
import subprocess
import aiohttp
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive + ENV ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та створення бота ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID   = 1160843618433630228
ADMIN_CHANNEL_ID = 1395065909185478769

# sessions: message_id → {
#   title: str,
#   lines: [str],
#   owners: [Member|None],
#   channel_id: int
# }
sessions: dict[int, dict] = {}

# claims: (message_id, slot_index) → [User, ...]
claims: dict[tuple[int,int], list[discord.User]] = {}

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"


# ─── 4. VTG Reminder ───────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except Exception as e:
                print(f"[vtg_reminder] Error sending reminder: {e}")


# ─── 5. Embed Builder ──────────────────────────────────────────────────────────
def build_embed(session: dict) -> discord.Embed:
    e = discord.Embed(title=session["title"], color=discord.Color.blue())
    desc = []
    for i, (line, owner) in enumerate(zip(session["lines"], session["owners"])):
        if owner:
            desc.append(f"{i+1}. {line}  –  Зайнято {owner.mention}")
        else:
            desc.append(f"{i+1}. {line}")
    e.description = "\n".join(desc)
    return e


# ─── 6. Slot Buttons & View ────────────────────────────────────────────────────
class SlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        self.session_id = session_id
        self.idx = idx
        owner = sessions[session_id]["owners"][idx]
        free = owner is None
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label,
                         style=style,
                         custom_id=f"slot-{session_id}-{idx}")

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        channel_id = sess["channel_id"]

        # Вільний слот → зайняти, але лише один слот в цій гілці
        if owner is None:
            # Перевіряємо всі сесії в тій самій гілці
            for sid, other in sessions.items():
                if other["channel_id"] == channel_id and user in other["owners"]:
                    return await interaction.response.send_message(
                        "⚠️ Ви вже зайняли слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            await interaction.response.edit_message(
                embed=build_embed(sess),
                view=SlotView(self.session_id)
            )
            return

        # Ваш слот → звільнити
        if owner == user:
            sess["owners"][self.idx] = None
            await interaction.response.edit_message(
                embed=build_embed(sess),
                view=SlotView(self.session_id)
            )
            return

        # Чужий слот → пропонуємо претендувати
        view = ClaimSlotView(self.session_id, self.idx)
        await interaction.response.send_message(
            f"⚠️ Цей слот вже зайнято {owner.mention}.", view=view, ephemeral=True
        )


class SlotView(View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[session_id]["lines"])):
            self.add_item(SlotButton(session_id, idx))


# ─── 7. Claim “Претендувати” ───────────────────────────────────────────────────
class ClaimSlotButton(Button):
    def __init__(self, session_id: int, idx: int):
        self.session_id = session_id
        self.idx = idx
        super().__init__(label="❗ Претендувати",
                         style=discord.ButtonStyle.primary,
                         custom_id=f"claim-slot-{session_id}-{idx}")

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        key = (self.session_id, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await interaction.response.send_message(
                "ℹ️ Ви вже претендували на цей слот.", ephemeral=True
            )
        lst.append(user)
        await interaction.response.send_message(
            "✅ Заявка надіслана адміністрації.", ephemeral=True
        )

        # Повідомляємо адміністраторів
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            embed = discord.Embed(
                title="Нова заявка на слот",
                color=discord.Color.orange()
            )
            embed.add_field(name="Сесія", value=sess["title"], inline=False)
            embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
            embed.add_field(name="Власник", value=owner.mention if owner else "Ніхто", inline=True)
            embed.add_field(name="Претендент", value=user.mention, inline=False)
            view = ClaimDecisionView(self.session_id, self.idx, user.id)
            await admin_ch.send(embed=embed, view=view)


class ClaimSlotView(View):
    def __init__(self, session_id: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(session_id, idx))


# ─── 8. Admin Decision ─────────────────────────────────────────────────────────
class ClaimDecisionButton(Button):
    def __init__(self, session_id: int, idx: int, claimant_id: int, accept: bool):
        self.session_id = session_id
        self.idx = idx
        self.claimant_id = claimant_id
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(label=label,
                         style=style,
                         custom_id=f"claim-{tag}-{session_id}-{idx}-{claimant_id}")

    async def callback(self, interaction: discord.Interaction):
        sess = sessions[self.session_id]
        key = (self.session_id, self.idx)
        claimant = bot.get_user(self.claimant_id)
        owner = sess["owners"][self.idx]

        # Призначити нового власника
        if self.custom_id.startswith("claim-accept"):
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)

            # Оновити оригінальне повідомлення з слотами
            orig_ch = bot.get_channel(sess["channel_id"])
            if orig_ch:
                try:
                    orig_msg = await orig_ch.fetch_message(self.session_id)
                    await orig_msg.edit(embed=build_embed(sess), view=SlotView(self.session_id))
                except Exception as e:
                    print(f"[ClaimDecision] can't edit original msg: {e}")

            # Сповіщення колишньому власнику
            if owner and owner != claimant:
                try:
                    await owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} у «{sess['title']}» передано {claimant.mention}."
                    )
                except: pass

            # Сповіщення новому власнику
            try:
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}»."
                )
            except: pass

            await interaction.response.send_message("✔️ Слот успішно перепризначено.", ephemeral=True)

        # Відхилити заявку
        else:
            lst = claims.get(key, [])
            if claimant in lst:
                lst.remove(claimant)

            try:
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена."
                )
            except: pass

            await interaction.response.send_message("✖️ Заявку відхилено.", ephemeral=True)


class ClaimDecisionView(View):
    def __init__(self, session_id: int, idx: int, claimant_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, accept=True))
        self.add_item(ClaimDecisionButton(session_id, idx, claimant_id, accept=False))


# ─── 9. Events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] Bot {bot.user}")
    # Запускаємо просто один раз
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    lines = message.content.splitlines()
    if any("запис слоти" in L.lower() for L in lines):
        header, slots, owners = None, [], []
        for raw in lines:
            txt = raw.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                # знаходимо власника через mentions
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

        slots  = slots[:25]
        owners = owners[:len(slots)]

        session = {
            "title":   header or DEFAULT_TITLE,
            "lines":   slots,
            "owners":  owners,
            "channel_id": message.channel.id
        }
        embed = build_embed(session)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = session
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)


# ─── 10. Commands ───────────────────────────────────────────────────────────────
@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    emb.add_field(name="Sessions", value=str(len(sessions)), inline=True)
    emb.add_field(name="Claims",   value=str(sum(len(v) for v in claims.values())), inline=True)
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
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\tas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`", inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)


# ─── 11. Старт ─────────────────────────────────────────────────────────────────
bot.run(TOKEN)