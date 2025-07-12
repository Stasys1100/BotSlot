import os
import re
import subprocess
import aiohttp
import datetime
import discord

from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive + завантаження ENV ─────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти і створення бота ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Зберігаємо стан усіх “відділень” у sessions ───────────────────────────
# ключ = message.id, значення = dict { title, lines: [str], owners: [User|None] }
sessions: dict[int, dict] = {}
TRIGGER = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"


def build_embed(session: dict) -> discord.Embed:
    """Створює Embed по даним конкретної сесії."""
    e = discord.Embed(title=session["title"], color=discord.Color.blue())
    lines = []
    for text, owner in zip(session["lines"], session["owners"]):
        lines.append(f"{text} – Зайнято {owner.mention}" if owner else text)
    e.description = "\n".join(lines)
    return e


class SlotButton(Button):
    """Button із прив’язкою до тієї самої сесії (message_id) і свого index."""
    def __init__(self, message_id: int, idx: int, row: int):
        self.session_id = message_id
        self.idx = idx
        session = sessions[message_id]
        owner = session["owners"][idx]
        free = owner is None

        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger

        super().__init__(
            label=label,
            style=style,
            custom_id=f"slot-{message_id}-{idx}",
            row=row
        )

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        session = sessions[self.session_id]
        owner = session["owners"][self.idx]

        # 1) Якщо вільний, і ви ще ніде не записані → зайняти
        if owner is None:
            if user in session["owners"]:
                return await inter.response.send_message(
                    "⚠️ Ви вже зайняли слот в цьому відділенні.", ephemeral=True
                )
            session["owners"][self.idx] = user

        # 2) Якщо ви власник цього слота → звільнити
        elif owner == user:
            session["owners"][self.idx] = None

        # 3) Якщо це чужий слот → заборона
        else:
            return await inter.response.send_message(
                "⚠️ Ви не можете звільнити чужий слот.", ephemeral=True
            )

        # Редагуємо лише це повідомлення
        await inter.response.edit_message(
            embed=build_embed(session),
            view=SlotView(self.session_id)
        )


class SlotView(View):
    """View із кнопками для конкретного message_id."""
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        count = len(sessions[message_id]["lines"])
        for idx in range(count):
            row = idx // 5
            self.add_item(SlotButton(message_id, idx, row))


@bot.event
async def on_ready():
    print(f"[on_ready] Logged in as {bot.user} @ {datetime.datetime.utcnow().isoformat()} UTC")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit `{commit}`",
        color=discord.Color.green()
    )
    for g in bot.guilds:
        # перший доступний канал з правом send_messages
        ch = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
                      and c.permissions_for(g.me).send_messages,
            g.text_channels
        )
        if ch:
            try:
                await ch.send(embed=emb)
            except:
                pass


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    # Ловимо будь-яке “запис слоти” у тексті
    lines = msg.content.splitlines()
    if any("запис слоти" in L.lower() for L in lines):
        header = None
        slots = []

        for raw in lines:
            text = raw.strip()
            if not text or "запис слоти" in text.lower() or "everyone" in text.lower():
                continue
            m = TRIGGER.match(text)
            if m:
                slots.append(text)
            elif header is None:
                header = text

        slots = slots[:25]
        session = {
            "title": header or DEFAULT_TITLE,
            "lines": slots,
            "owners": [None] * len(slots)
        }

        # Відправляємо Embed, реєструємо session під message.id і додаємо View
        embed = build_embed(session)
        sent = await msg.channel.send(embed=embed)
        sessions[sent.id] = session
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(msg)


# ─── Ось ваші команди ──────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показати слоти, де ви зараз записані (усі відділення)."""
    out = []
    for sid, sess in sessions.items():
        taken = [
            sess["lines"][i]
            for i, u in enumerate(sess["owners"])
            if u == ctx.author
        ]
        if taken:
            out.append(f"**{sess['title']}**\n" + "\n".join(taken))

    if out:
        await ctx.send("\n\n".join(out))
    else:
        await ctx.send("🕸 Ви не записані в жодне відділення.")


@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 message_content intent = `{bot.intents.message_content}`\n"
        f"🗂 Guilds: {guilds}\n"
        f"🔑 Active sessions: {len(sessions)}"
    )


@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    emb.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=emb)


@bot.command()
async def оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Render-деплой тригерено!")


@bot.command()
async def gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки",
                  value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add", value="`git add .`", inline=False)
    emb.add_field(name="3. git commit",
                  value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)


# ─── 9. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)