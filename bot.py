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

# ─── 2. Інтенти і створення бота ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Список слотів (будь-якої довжини) ───────────────────────────────────────
slot_lines = [
    "1. Ryhmäjohtaja/Командир відділення (RK-95)",
    "2: Tarkka-ampuja/Марксмен (RK-95)",
    "3. Panssarintorjunta-ampuja/Гранатометник (RK-95\\M136)",
    "4: Partionjohtaja/Лідер групи (RK-95)",
    "5. Konekivääriampuja/Кулеметник (PKM)",
    "6: Kivääriampuja/Стрілець (RK-95)",
    "7: Tiiminjohtaja/Лідер групи (RK-95)",
    "8. Konekivääriampuja/Кулеметник (PKM)",
    "9: Taistelusairaanhoitaja/Медик (RK-95) | MED",
    # Додавайте стільки рядків, скільки треба — бот підтримує до 25 слотів
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

# ─── 5. Будуємо Embed: кожен слот на новому рядку, поруч “– Зайнято @User” ─────────
def make_embed() -> discord.Embed:
    embed = discord.Embed(title=EMBED_TITLE, color=discord.Color.blue())
    lines = []
    for idx, full_line in enumerate(slot_lines):
        if not is_slot(full_line):
            continue
        user = slot_users[idx]
        if user:
            lines.append(f"{full_line} – Зайнято {user.mention}")
        else:
            lines.append(full_line)
    embed.description = "\n".join(lines)
    return embed

# ─── 6. View та Button (один слот на користувача) ───────────────────────────────
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for idx, full_line in enumerate(slot_lines):
            if is_slot(full_line):
                # до 5 кнопок у рядку → idx//5
                row = idx // 5
                self.add_item(SlotButton(idx, row))

class SlotButton(Button):
    def __init__(self, index: int, row: int):
        self.index = index
        free = slot_users[index] is None
        label = f"{index+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style,
                         custom_id=f"slot_{index}", row=row)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        occupied = [i for i, u in enumerate(slot_users) if u == user]

        # Намагаємось зайняти вільний слот
        if slot_users[self.index] is None:
            if occupied:
                old = occupied[0] + 1
                return await interaction.response.send_message(
                    f"⚠️ Ви вже зайняті у слоті {old}. Спочатку звільніть його.",
                    ephemeral=True
                )
            slot_users[self.index] = user
            await interaction.response.send_message(
                f"✅ Ви зайняли слот {self.index+1}", ephemeral=True
            )

        # Звільняємо свій слот
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(
                f"❌ Ви звільнили слот {self.index+1}", ephemeral=True
            )

        # Чужий слот
        else:
            return await interaction.response.send_message(
                "⚠️ Цей слот зайнятий іншим користувачем", ephemeral=True
            )

        # Після зміни оновлюємо Embed+View
        await interaction.message.edit(embed=make_embed(), view=SlotView())

# ─── 7. Події ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] Старт @ {datetime.datetime.utcnow().isoformat()} UTC")
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
    """Показує, у якому слоті ви зараз записані."""
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == ctx.author and is_slot(slot_lines[i])
    ]
    msg = ("🎯 Ви записані у:\n" + "\n".join(taken)) if taken else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

@bot.command()
async def debug(ctx: commands.Context):
    """Діагностика: intent, сервери, кількість слотів."""
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent = `{bot.intents.message_content}`\n"
        f"🗂 Guilds: {guilds}\n"
        f"ℹ️ Slots: {len(slot_lines)}"
    )

@bot.command()
async def статус(ctx: commands.Context):
    """Показує commit, токен і webhook-посилання."""
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    emb.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=emb)

@bot.command()
async def оновити(ctx: commands.Context):
    """Тригер нового деплою через Render webhook."""
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Оновлення тригерено!")

@bot.command()
async def gitpush(ctx: commands.Context):
    """Інструкція: git add → commit → push → !оновити."""
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. Перейти в папку", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    emb.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. Push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=emb)

# ─── 9. Старт бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)