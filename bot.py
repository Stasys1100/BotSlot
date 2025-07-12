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

# ─── 1. Keep-alive і .env ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтент і створення бота ─────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Глобальні: тут зберігаємо пари (рядок-слот, хто зайняв) ─────────────────
slot_pattern = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
slot_lines: list[str] = []
slot_users: list[discord.User | None] = []

EMBED_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Відповідність “N.” / “N:” → валідний слот ───────────────────────────────
def make_embed() -> discord.Embed:
    e = discord.Embed(title=EMBED_TITLE, color=discord.Color.blue())
    lines: list[str] = []
    for idx, text in enumerate(slot_lines):
        user = slot_users[idx]
        if user:
            lines.append(f"{text} – Зайнято {user.mention}")
        else:
            lines.append(text)
    e.description = "\n".join(lines)
    return e

# ─── 5. Кнопки у View, по 5 у рядку ─────────────────────────────────────────────
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for idx in range(len(slot_lines)):
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

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        occupied = [i for i, u in enumerate(slot_users) if u == user]

        # зайняти
        if slot_users[self.index] is None:
            if occupied:
                no = occupied[0] + 1
                return await inter.response.send_message(
                    f"⚠️ Ви вже в слоті {no}. Спочатку звільніть його.",
                    ephemeral=True
                )
            slot_users[self.index] = user

        # звільнити
        elif slot_users[self.index] == user:
            slot_users[self.index] = None

        # чужий слот
        else:
            return await inter.response.send_message(
                "⚠️ Цей слот уже зайнятий", ephemeral=True
            )

        # редагуємо **єдине** повідомлення
        await inter.response.edit_message(embed=make_embed(), view=SlotView())

# ─── 6. Ловимо “запис слоти” у повідомленні й парсимо його ────────────────────────
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    if "запис слоти" in msg.content.lower():
        global slot_lines, slot_users
        raw = msg.content.splitlines()
        parsed = []
        for line in raw:
            m = slot_pattern.match(line)
            if m:
                parsed.append(line.strip())
        slot_lines = parsed[:25]
        slot_users = [None] * len(slot_lines)
        await msg.channel.send(embed=make_embed(), view=SlotView())

    await bot.process_commands(msg)

# ─── 7. Ваші команди ────────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    taken = [slot_lines[i] for i, u in enumerate(slot_users) if u == ctx.author]
    msg = "🎯 Ви в слоті:\n" + "\n".join(taken) if taken else "🕸 Ви ніде не записані"
    await ctx.send(msg)

@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent.message_content = `{bot.intents.message_content}`\n"
        f"🗂 Servers: {guilds}\n"
        f"ℹ️ Slots parsed: {len(slot_lines)}"
    )

@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    e = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    e.add_field(name="Commit", value=commit, inline=True)
    e.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    e.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=e)

@bot.command()
async def оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Render-деплой тригерено!")

@bot.command()
async def gitpush(ctx: commands.Context):
    e = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    e.add_field(name="1. cd до папки", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    e.add_field(name="2. git add", value="`git add .`", inline=False)
    e.add_field(name="3. git commit", value='`git commit -m "Оновлення слота"`', inline=False)
    e.add_field(name="4. git push", value="`git push origin main`", inline=False)
    e.set_footer(text="Після push → !оновити")
    await ctx.send(embed=e)

# ─── 8. Старт бота ───────────────────────────────────────────────────────────────
bot.run(TOKEN)