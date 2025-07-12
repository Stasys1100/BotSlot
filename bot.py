import os
import subprocess
import aiohttp
import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive і .env ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти і бот ───────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Список слотів прямо в коді (будь-якої довжини) ─────────────────────────
slot_lines = [
    "1. Командир відділення (RK-95)",
    "2: Марксмен (RK-95)",
    "3. Гранатометник (RK-95/M136)",
    "4: Лідер групи (RK-95)",
    "5. Кулеметник (PKM)"
    # ... можна додати будь-яку кількість рядків зі своєю нумерацією
]
slot_users = [None] * len(slot_lines)

EMBED_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Фільтр: рядок починається з “N.” або “N:”? ────────────────────────────────
def is_slot(line: str) -> bool:
    text = line.strip()
    for n in range(1, len(slot_lines) + 1):
        if text.startswith(f"{n}.") or text.startswith(f"{n}:"):
            return True
    return False

# ─── 5. Динамічна побудова кнопок ───────────────────────────────────────────────
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        row = 0
        for idx, line in enumerate(slot_lines):
            if is_slot(line):
                self.add_item(SlotButton(idx, row))
                row += 1

class SlotButton(Button):
    def __init__(self, index: int, row: int):
        free = slot_users[index] is None
        label = f"{index+1}. {'Вільний' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style,
                         custom_id=f"slot_{index}", row=row)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(
                f"✅ Ви записались у слот {self.index+1}", ephemeral=True
            )
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(
                f"❌ Ви відмовились від слота {self.index+1}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Цей слот уже зайнятий", ephemeral=True
            )
            return
        await interaction.message.edit(embed=make_embed(), view=SlotView())

# ─── 6. Формуємо embed ───────────────────────────────────────────────────────────
def make_embed() -> discord.Embed:
    embed = discord.Embed(title=EMBED_TITLE, color=discord.Color.blue())
    lines = []
    for idx, line in enumerate(slot_lines):
        if is_slot(line):
            mention = slot_users[idx].mention if slot_users[idx] else ""
            lines.append(f"{line}\n{mention}")
    embed.description = "\n".join(lines)
    return embed

# ─── 7. Події ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] Старт бота @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"[on_ready] message_content intent = {bot.intents.message_content}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(
                        embed=discord.Embed(
                            title="🔄 Бот перезапущено",
                            description=f"📦 Commit: `{commit}`",
                            color=discord.Color.green()
                        )
                    )
                    return
                except:
                    continue

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    print(f"[on_message] {message.author}: {message.content}")
    if "запис слоти" in message.content.lower():
        global slot_users
        slot_users = [None] * len(slot_lines)
        await message.channel.send(embed=make_embed(), view=SlotView())
    await bot.process_commands(message)

# ─── 8. Ваші команди ───────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == ctx.author and is_slot(slot_lines[i])
    ]
    msg = "🎯 Ви записані у:\n" + "\n".join(taken) if taken else "🕸 Ви не записані"
    await ctx.send(msg)

@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent = `{bot.intents.message_content}`\n"
        f"🗂 Servers: {guilds}\n"
        f"ℹ️ Slots: {len(slot_lines)}"
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
    await ctx.send("🔄 Оновлення тригерено!")

@bot.command()
async def gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd в папку", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add", value="`git add .`", inline=False)
    emb.add_field(name="3. git commit", value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 9. Старт бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN