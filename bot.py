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

# ─── 1. Keep-alive і завантаження .env ─────────────────────────────────────────
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та створення бота ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Регекс для парсингу слотів і змінні ────────────────────────────────────
slot_pattern  = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
slot_lines    = []   # ["1. Slot A", "2: Slot B", ...]
slot_users    = []   # [User|None, ...]
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"
embed_title   = DEFAULT_TITLE

# ─── 4. Функція для побудови чистого Embed без порожніх рядків ────────────────
def make_embed() -> discord.Embed:
    e = discord.Embed(title=embed_title, color=discord.Color.blue())
    lines = []
    for idx, text in enumerate(slot_lines):
        user = slot_users[idx]
        if user:
            lines.append(f"{text} – Зайнято {user.mention}")
        else:
            lines.append(text)
    e.description = "\n".join(lines)
    return e

# ─── 5. View і Button (по 5 кнопок у рядку, 1 слот на користувача) ───────────
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

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        current_owner = slot_users[self.index]

        # якщо слот вільний – пробуємо зайняти
        if current_owner is None:
            # перевірка: у користувача ще немає свого слота
            if any(u == user for u in slot_users):
                # вже має слот → блок
                return await interaction.response.send_message(
                    "⚠️ Ви вже зайняли інший слот. Спершу звільніть його.",
                    ephemeral=True
                )
            slot_users[self.index] = user

        # якщо власник – звільняємо
        elif current_owner == user:
            slot_users[self.index] = None

        # якщо хтось інший – не даємо відмовитися
        else:
            return await interaction.response.send_message(
                "⚠️ Ви не можете звільнити чужий слот.", ephemeral=True
            )

        # редагуємо одне повідомлення без додаткових нотіфікацій
        await interaction.response.edit_message(embed=make_embed(), view=SlotView())

# ─── 6. on_ready: розсилаємо повідомлення про перезапуск у всі гільдії ─────────
@bot.event
async def on_ready():
    print(f"[on_ready] Бот запущено @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"[on_ready] message_content intent = {bot.intents.message_content}")
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
            except Exception as e:
                print(f"[on_ready] Cannot send to {guild.name}/{channel.name}: {e}")

# ─── 7. on_message: ловимо “запис слоти”, парсимо заголовок і слоти ────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    lines = message.content.splitlines()
    if any("запис слоти" in l.lower() for l in lines):
        global slot_lines, slot_users, embed_title

        parsed = []
        header = None

        for raw in lines:
            text = raw.strip()
            if not text:
                continue
            # пропускаємо тригер та @everyone
            if "запис слоти" in text.lower() or "everyone" in text.lower():
                continue
            m = slot_pattern.match(text)
            if m:
                parsed.append(text)
            elif header is None:
                header = text

        slot_lines = parsed[:25]
        slot_users = [None] * len(slot_lines)
        embed_title = header or DEFAULT_TITLE

        # надсилаємо єдине повідомлення Embed+View
        await message.channel.send(embed=make_embed(), view=SlotView())

    await bot.process_commands(message)

# ─── 8. Команди користувача ───────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, у якому слоті ви зараз записані."""
    taken = [slot_lines[i] for i, u in enumerate(slot_users) if u == ctx.author]
    if taken:
        await ctx.send("🎯 Ви записані у:\n" + "\n".join(taken))
    else:
        await ctx.send("🕸 Ви не записані у жоден слот")

@bot.command()
async def debug(ctx: commands.Context):
    """Діагностика: intent, сервери, кількість слотів."""
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent = `{bot.intents.message_content}`\n"
        f"🗂 Servers: {guilds}\n"
        f"ℹ️ Slots parsed: {len(slot_lines)}"
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
    emb.add_field(name="1. cd до папки",   value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                             inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`',       inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",                 inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 9. Старт бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)