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

# ─── 2. Інтент і створення бота ─────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Зчитуємо слоти з файлу slots.txt або беремо список за замовчуванням ────
SLOT_FILE = "slots.txt"
if os.path.exists(SLOT_FILE):
    with open(SLOT_FILE, encoding="utf-8") as f:
        slot_lines = [line.strip() for line in f if line.strip()]
else:
    slot_lines = [
        "1. Командир відділення (RK-95)",
        "2: Марксмен (RK-95)",
        "3. Гранатометник (Rk-95/M136)",
        "4: Лідер групи (RK-95)",
        "5. Кулеметник (PKM)",
        "6: Стрілець (RK-95)",
        "7. Лідер групи (RK-95)",
        "8. Кулеметник (PKM)",
        "9: Медик (RK-95)"
    ]

# Список поточно зайнятих слотів (None → вільний)
slot_users = [None] * len(slot_lines)

# ─── 4. Функція-фільтр: рядок починається з "N." або "N:"? ─────────────────────────
def is_slot(line: str) -> bool:
    text = line.strip()
    for n in range(1, len(slot_lines) + 1):
        if text.startswith(f"{n}.") or text.startswith(f"{n}:"):
            return True
    return False

# ─── 5. View та Button для динамічного створення ────────────────────────────────
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
        # 1) Якщо вільний – займаємо
        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(
                f"✅ Ви записались у слот {self.index+1}", ephemeral=True
            )
        # 2) Якщо це ви ж – відмовляєтесь
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(
                f"❌ Ви відмовились від слота {self.index+1}", ephemeral=True
            )
        # 3) Якщо зайнятий іншим – попереджаємо
        else:
            await interaction.response.send_message(
                "⚠️ Цей слот вже зайнятий іншим користувачем", ephemeral=True
            )
            return

        # Після зміни – оновлюємо повідомлення
        await interaction.message.edit(content=format_slots(), view=SlotView())

# Формуємо текст із валідними слотами та mentions
def format_slots() -> str:
    header = "**Запис у слоти**\n"
    body = ""
    for idx, line in enumerate(slot_lines):
        if is_slot(line):
            mention = slot_users[idx].mention if slot_users[idx] else ""
            body += f"{line}\n{mention}\n"
    return header + body

# ─── 6. Слухаємо події ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[on_ready] Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"[on_ready] message_content intent = {bot.intents.message_content}")
    # Повідомлення про релонч у першому доступному текстовому каналі
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(f"🔄 Бот перезапущено\n📦 Commit: `{commit}`")
                    return
                except:
                    continue

@bot.event
async def on_message(message: discord.Message):
    # ‣ Ігноруємо боти
    if message.author.bot:
        return

    # ‣ Лог у консоль для діагностики
    print(f"[on_message] {message.author}: {message.content}")

    # ‣ Якщо це команда (починається на префікс) – пропускаємо на обробку команд
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    # ‣ Якщо в тексті є “запис слоти” – показуємо список кнопок
    if "запис слоти" in message.content.lower():
        global slot_users
        slot_users = [None] * len(slot_lines)
        await message.channel.send(content=format_slots(), view=SlotView())

    # ‣ В будь-якому випадку пропускаємо командний цикл
    await bot.process_commands(message)

# ─── 7. Додаткові команди ─────────────────────────────────────────────────────

@bot.command()
async def debug(ctx: commands.Context):
    """
    Віддає базову діагностику:
     • message_content intent
     • список серверів
     • кількість слотів
    """
    guilds = ", ".join(g.name for g in bot.guilds)
    await ctx.send(
        f"🔍 message_content intent = `{bot.intents.message_content}`\n"
        f"🗂 Guilds: {guilds}\n"
        f"ℹ️ Slot count = {len(slot_lines)}"
    )

@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=commit, inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        async with sess.post(DEPLOY_HOOK_URL):
            await ctx.send("🔄 Оновлення викликано! Render запускає нову версію…")

@bot.command()
async def gitpush(ctx: commands.Context):
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку",
                    value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    embed.add_field(name="4. Push", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=embed)

# ─── 8. Стартуємо бота ─────────────────────────────────────────────────────────
bot.run(TOKEN)