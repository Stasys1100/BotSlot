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

# ─── 1. Keep-alive і завантаження .env ───────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти і створення бота ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Сесії: message_id → { title, lines, users } ─────────────────────────────
sessions: dict[int, dict] = {}
TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

def build_embed(session: dict) -> discord.Embed:
    """Повертає Embed для однієї сесії слотування."""
    e = discord.Embed(title=session["title"], color=discord.Color.blue())
    e.description = "\n".join(
        f"{line} – Зайнято {user.mention}" if user else line
        for line, user in zip(session["lines"], session["users"])
    )
    return e

class SlotButton(Button):
    """Кнопка зайняти/відмовитись від слота для конкретної сесії."""
    def __init__(self, session_id: int, idx: int, row: int):
        session = sessions[session_id]
        free = session["users"][idx] is None
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(
            label=label,
            style=style,
            custom_id=f"slot_{session_id}_{idx}",
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        # Розбираємо сесію й індекс слота з custom_id
        _, sid, idx = interaction.data["custom_id"].split("_")
        sid, idx = int(sid), int(idx)
        session = sessions[sid]
        user = interaction.user
        owner = session["users"][idx]

        # 1) Якщо слот вільний → спроба зайняти
        if owner is None:
            if user in session["users"]:
                return await interaction.response.send_message(
                    "⚠️ Ви вже зайняли слот у цьому відділенні. Спершу звільніть його.",
                    ephemeral=True
                )
            session["users"][idx] = user

        # 2) Якщо ви власник цього слота → звільнити
        elif owner == user:
            session["users"][idx] = None

        # 3) Якщо слот зайнятий іншим → блок
        else:
            return await interaction.response.send_message(
                "⚠️ Ви не можете звільнити чужий слот.", ephemeral=True
            )

        # Оновлюємо лише це повідомлення
        await interaction.response.edit_message(
            embed=build_embed(session),
            view=SlotView(sid)
        )

class SlotView(View):
    """View із кнопками для всіх слотів конкретної сесії."""
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        lines = sessions[session_id]["lines"]
        for idx in range(len(lines)):
            row = idx // 5
            self.add_item(SlotButton(session_id, idx, row))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    lines = message.content.splitlines()
    if any("запис слоти" in l.lower() for l in lines):
        # Парсимо заголовок і слоти
        header = None
        slots = []
        for raw in lines:
            txt = raw.strip()
            if not txt:
                continue
            # Пропускаємо тригер і everyone
            if "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                slots.append(txt)
            elif header is None:
                header = txt

        slots = slots[:25]
        session = {
            "title": header or DEFAULT_TITLE,
            "lines": slots,
            "users": [None] * len(slots)
        }

        # Відправляємо Embed, зберігаємо сесію та додаємо View
        embed = build_embed(session)
        sent = await message.channel.send(embed=embed)
        sid = sent.id
        sessions[sid] = session
        await sent.edit(view=SlotView(sid))

    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"[on_ready] Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    restart_embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    for guild in bot.guilds:
        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
                       and c.permissions_for(guild.me).send_messages,
            guild.text_channels
        )
        if channel:
            try:
                await channel.send(embed=restart_embed)
            except:
                pass

# ─── Команди користувача ──────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, у яких слотах ви записані в кожному відділенні."""
    output = []
    for sid, sess in sessions.items():
        taken = [
            sess["lines"][i]
            for i, u in enumerate(sess["users"])
            if u == ctx.author
        ]
        if taken:
            output.append(f"**{sess['title']}**\n" + "\n".join(taken))

    if output:
        await ctx.send("\n\n".join(output))
    else:
        await ctx.send("🕸 Ви не записані у жоден слот")

@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent.message_content = `{bot.intents.message_content}`\n"
        f"🗂 Servers: {guilds}\n"
        f"ℹ️ Active sessions: {len(sessions)}"
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
    emb.add_field(name="1. cd до папки",   value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                             inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`',       inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",                 inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── Запуск бота ────────────────────────────────────────────────────────────────
bot.run(TOKEN)