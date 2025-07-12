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

# ─── 3. Глобальні змінні (заповнюються при "запис слоти") ───────────────────────
slot_lines: list[str] = []
slot_users: list[discord.User | None] = []

EMBED_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Визначаємо слот-рядок: починається з “N.” або “N:” ────────────────────────
slot_pattern = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
def is_slot_line(text: str) -> bool:
    return bool(slot_pattern.match(text))

# ─── 5. Створюємо Embed, кожен слот — новий рядок, поруч “– Зайнято @User” ───────
def make_embed() -> discord.Embed:
    embed = discord.Embed(title=EMBED_TITLE, color=discord.Color.blue())
    lines: list[str] = []
    for idx, full_line in enumerate(slot_lines):
        user = slot_users[idx]
        if user:
            lines.append(f"{full_line} – Зайнято {user.mention}")
        else:
            lines.append(full_line)
    embed.description = "\n".join(lines)
    return embed

# ─── 6. View та Button ─────────────────────────────────────────────────────────
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for idx in range(len(slot_lines)):
            # row: 0… row_max, по 5 кнопок у рядку
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

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        # користувач може зайняти тільки один слот
        my_slots = [i for i, u in enumerate(slot_users) if u == user]

        # якщо слот вільний та у користувача ще немає слота
        if slot_users[self.index] is None:
            if my_slots:
                slot_no = my_slots[0] + 1
                return await interaction.response.send_message(
                    f"⚠️ Ви вже зайняли слот {slot_no}. "
                    "Спочатку звільніть його.", ephemeral=True
                )
            slot_users[self.index] = user

        # якщо це його слот – звільняємо
        elif slot_users[self.index] == user:
            slot_users[self.index] = None

        # чужий слот
        else:
            return await interaction.response.send_message(
                "⚠️ Цей слот зайнятий іншим користувачем", ephemeral=True
            )

        # редагуємо **єдине** повідомлення (без додаткових “✅ Ви зайняли…”)
        await interaction.response.edit_message(
            embed=make_embed(), view=SlotView()
        )

# ─── 7. Обробляємо вхідні повідомлення ─────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    text = message.content.strip()
    # ловимо фразу “запис слоти” і парсимо усі рядки-слоти
    if "запис слоти" in text.lower():
        global slot_lines, slot_users

        # парсимо всі рядки повідомлення
        raw_lines = message.content.splitlines()
        parsed = [line.strip() for line in raw_lines if is_slot_line(line)]
        # підтримка максимум 25 слотів (Discord UI обмеження)
        slot_lines = parsed[:25]
        slot_users = [None] * len(slot_lines)

        # надсилаємо одне повідомлення з embed + кнопки
        await message.channel.send(embed=make_embed(), view=SlotView())

    await bot.process_commands(message)

# ─── 8. Ваші команди ───────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, у якому слоті ви зараз записані."""
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == ctx.author
    ]
    msg = "🎯 Ви записані у:\n" + "\n".join(taken) \
          if taken else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

@bot.command()
async def debug(ctx: commands.Context):
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent.message_content = `{bot.intents.message_content}`\n"
        f"🗂 Guilds: {guilds}\n"
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
    emb.add_field(name="1. Перейти в папку", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    emb.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. Push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 9. Старт бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)