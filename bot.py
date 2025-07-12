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

# ─── 3. Регулярний вираз для парсингу слотів і глобальні змінні ────────────────
slot_pattern  = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
slot_lines    = []                              # List[str] із текстом кожного слота
slot_users    = []                              # List[User|None] відповідний заповнювач
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"
embed_title   = DEFAULT_TITLE                   # Заголовок ембеда, змінюється динамічно

# ─── 4. Функція для побудови одного Embed без порожніх ліній ────────────────────
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

# ─── 5. View + Button для слотів (по 5 кнопок у рядку, 1 слот на користувача) ──
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
        occupied = [i for i, u in enumerate(slot_users) if u == user]

        # Спроба зайняти слот
        if slot_users[self.index] is None:
            if occupied:
                no = occupied[0] + 1
                return await interaction.response.send_message(
                    f"⚠️ Ви вже в слоті {no}. Спершу звільніть його.",
                    ephemeral=True
                )
            slot_users[self.index] = user

        # Спроба звільнити власний слот
        elif slot_users[self.index] == user:
            slot_users[self.index] = None

        # Слот зайнятий іншим
        else:
            return await interaction.response.send_message(
                "⚠️ Цей слот уже зайнятий", ephemeral=True
            )

        # Редагуємо **єдине** повідомлення
        await interaction.response.edit_message(embed=make_embed(), view=SlotView())

# ─── 6. Подія on_ready: повідомлення про перезапуск у всіх доступних каналах ─────
@bot.event
async def on_ready():
    print(f"[on_ready] Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"[on_ready] message_content intent = {bot.intents.message_content}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    restart_embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    for guild in bot.guilds:
        channel = discord.utils.find(
            lambda c: (
                isinstance(c, discord.TextChannel)
                and c.permissions_for(guild.me).send_messages
            ),
            guild.text_channels
        )
        if channel:
            try:
                await channel.send(embed=restart_embed)
            except Exception as e:
                print(f"[on_ready] Failed to send to {guild.name}/{channel.name}: {e}")

# ─── 7. Подія on_message: ловимо «запис слоти» і парсимо ваше повідомлення ─────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content_lines = message.content.splitlines()
    if any("запис слоти" in l.lower() for l in content_lines):
        global slot_lines, slot_users, embed_title

        parsed = []
        header = None

        for raw in content_lines:
            text = raw.strip()
            if not text:
                continue
            # Пропускаємо тригер та everyone
            if "запис слоти" in text.lower() or "everyone" in text.lower():
                continue
            m = slot_pattern.match(text)
            if m:
                # Забираємо оригінальний рядок з номером та описом
                parsed.append(text)
            elif header is None:
                # Перша нерольова лінія стає заголовком
                header = text

        slot_lines = parsed[:25]
        slot_users = [None] * len(slot_lines)
        embed_title = header or DEFAULT_TITLE

        # Відправляємо одне повідомлення з Embed + View
        await message.channel.send(embed=make_embed(), view=SlotView())

    await bot.process_commands(message)

# ─── 8. Ваші команди ────────────────────────────────────────────────────────────
@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, у якому слоті ви зараз записані."""
    taken = [slot_lines[i] for i, u in enumerate(slot_users) if u == ctx.author]
    if taken:
        text = "\n".join(taken)
        await ctx.send(f"🎯 Ви записані у:\n{text}")
    else:
        await ctx.send("🕸 Ви не записані у жоден слот")

@bot.command()
async def debug(ctx: commands.Context):
    """Діагностика: intent, сервери, кількість слотів."""
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 intent.message_content = `{bot.intents.message_content}`\n"
        f"🗂 Servers: {guilds}\n"
        f"ℹ️ Slots parsed: {len(slot_lines)}"
    )

@bot.command()
async def статус(ctx: commands.Context):
    """Показує поточний commit, стан токена та webhook."""
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

# ─── 9. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)