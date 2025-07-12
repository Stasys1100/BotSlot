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

# ─── 1. Keep-alive та .env ─────────────────────────────────────────────────────
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та створення бота ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Регулярки та змінні для сесій ───────────────────────────────────────────
TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# sessions: message_id → { "title": str, "lines": [str], "owners": [User|None] }
sessions: dict[int, dict] = {}


def build_embed(session: dict) -> discord.Embed:
    """Створює Embed для конкретної сесії слотування."""
    e = discord.Embed(title=session["title"], color=discord.Color.blue())
    desc = []
    for line, owner in zip(session["lines"], session["owners"]):
        if owner:
            desc.append(f"{line} – Зайнято {owner.mention}")
        else:
            desc.append(line)
    e.description = "\n".join(desc)
    return e


class SlotButton(Button):
    """Кнопка Зайняти/Відмовитись із прив’язкою до message_id та індексу слоту."""
    def __init__(self, message_id: int, idx: int, row: int):
        self.msg_id = message_id
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

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        session = sessions[self.msg_id]
        owner = session["owners"][self.idx]

        # Вільний слот → пробуємо зайняти
        if owner is None:
            # Перевірка: чи ви вже зайняли слот у цій сесії
            if any(u == user for u in session["owners"] if u):
                return await interaction.response.send_message(
                    "⚠️ Ви вже маєте свій слот у цьому відділенні.", ephemeral=True
                )
            session["owners"][self.idx] = user

        # Ваш слот → звільнити
        elif owner == user:
            session["owners"][self.idx] = None

        # Чужий слот → блок
        else:
            return await interaction.response.send_message(
                f"⚠️ Цей слот закріплено за {owner.mention}.", ephemeral=True
            )

        # Оновлюємо **тільки** це повідомлення
        await interaction.response.edit_message(
            embed=build_embed(session),
            view=SlotView(self.msg_id)
        )


class SlotView(View):
    """View із кнопками для певної сесії (message_id)."""
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        count = len(sessions[message_id]["lines"])
        for idx in range(count):
            row = idx // 5
            self.add_item(SlotButton(message_id, idx, row))


@bot.event
async def on_ready():
    print(f"[on_ready] Bot ready @ {datetime.datetime.utcnow().isoformat()} UTC")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(
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
                await ch.send(embed=emb)
            except:
                pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    lines = message.content.splitlines()
    if any("запис слоти" in l.lower() for l in lines):
        header = None
        slots: list[str] = []
        owners: list[discord.User | None] = []

        for raw in lines:
            text = raw.strip()
            if not text or "запис слоти" in text.lower() or "everyone" in text.lower():
                continue
            m = TRIGGER_RE.match(text)
            if m:
                slots.append(text)
                # знайти згадку користувача в тій же стрічці
                mention = MENTION_RE.search(text)
                if mention and message.guild:
                    uid = int(mention.group("id"))
                    owner = message.guild.get_member(uid)
                else:
                    owner = None
                owners.append(owner)
            elif header is None:
                header = text

        # обмеження до 25 слотів (UI-ліміт)
        slots = slots[:25]
        owners = owners[: len(slots)]

        session = {
            "title": header or DEFAULT_TITLE,
            "lines": slots,
            "owners": owners
        }

        embed = build_embed(session)
        sent = await message.channel.send(embed=embed)
        sessions[sent.id] = session
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)


# ─── Команди користувача ───────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, у яких слотах ви записані (усі відділення)."""
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
        await ctx.send("🕸 Ви не записані у жоден слот")


@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent.message_content = `{bot.intents.message_content}`\n"
        f"🗂 Guilds: {guilds}\n"
        f"🔑 Active sessions: {len(sessions)}"
    )


@bot.command()
async def статус(ctx: commands.Context):
    """Показує commit, стан токена та webhook."""
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    emb.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=emb)


@bot.command()
async def оновити(ctx: commands.Context):
    """Trigger нового деплою через Render webhook."""
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Render-деплой тригерено!")


@bot.command()
async def gitpush(ctx: commands.Context):
    """Покрокова інструкція: git add → commit → push → !оновити."""
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки",
                  value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add", value="`git add .`", inline=False)
    emb.add_field(name="3. git commit",
                  value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)


# ─── Старт бота ────────────────────────────────────────────────────────────────
bot.run(TOKEN)