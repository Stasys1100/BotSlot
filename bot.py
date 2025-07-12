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

# ─── 1. Keep-alive та .env ──────────────────────────────────────────────────────
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та створення бота ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Глобальні змінні ─────────────────────────────────────────────────────────
slot_pattern = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
slot_lines: list[str] = []
slot_users: list[discord.User | None] = []
embed_title: str = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Функція для побудови Embed ─────────────────────────────────────────────
def make_embed() -> discord.Embed:
    e = discord.Embed(title=embed_title, color=discord.Color.blue())
    for idx, text in enumerate(slot_lines):
        user = slot_users[idx]
        line = f"{text} – Зайнято {user.mention}" if user else text
        e.add_field(name="\u200b", value=line, inline=False)
    return e

# ─── 5. View та Button ─────────────────────────────────────────────────────────
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for idx in range(len(slot_lines)):
            row = idx // 5
            self.add_item(SlotButton(idx, row))

class SlotButton(Button):
    def __init__(self, index: int, row: int):
        self.index = index
        is_free = slot_users[index] is None
        label = f"{index+1}. {'Зайняти' if is_free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if is_free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style,
                         custom_id=f"slot_{index}", row=row)

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        occupied = [i for i, u in enumerate(slot_users) if u == user]

        # Зайняти слот
        if slot_users[self.index] is None:
            if occupied:
                no = occupied[0] + 1
                return await inter.response.send_message(
                    f"⚠️ Ви вже в слоті {no}. Спершу звільніть його.", ephemeral=True
                )
            slot_users[self.index] = user

        # Звільнити свій слот
        elif slot_users[self.index] == user:
            slot_users[self.index] = None

        # Слот зайнятий іншим
        else:
            return await inter.response.send_message(
                "⚠️ Цей слот уже зайнятий", ephemeral=True
            )

        # Оновлюємо одне повідомлення
        await inter.response.edit_message(embed=make_embed(), view=SlotView())

# ─── 6. Обробка «запис слоти» ───────────────────────────────────────────────────
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    content = msg.content.splitlines()
    if any("запис слоти" in line.lower() for line in content):
        global slot_lines, slot_users, embed_title

        parsed: list[str] = []
        header: str | None = None

        for raw in content:
            raw_stripped = raw.strip()
            if not raw_stripped:
                continue
            # Пропускаємо тригер
            if "запис слоти" in raw_stripped.lower():
                continue
            # Лінія-слот?
            if slot_pattern.match(raw_stripped):
                parsed.append(raw_stripped)
            # Перша нерольова лінія — заголовок ембеда
            elif header is None:
                header = raw_stripped

        # Limit to 25 slots (UI обмеження)
        slot_lines = parsed[:25]
        slot_users = [None] * len(slot_lines)
        embed_title = header or embed_title

        # Надсилаємо одне єдине повідомлення
        await msg.channel.send(embed=make_embed(), view=SlotView())

    await bot.process_commands(msg)

# ─── 7. Команди бота ───────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує слот, у якому ви зараз."""  
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == ctx.author
    ]
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