import os
import subprocess
import aiohttp
import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# Підтримка безперервного хостингу
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# Інтент для читання content повідомлень
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Список всіх рядків, серед яких є слоти
slot_lines = [
    "1. Ryhmäjohtaja/Командир відділення (RK-95)",
    "2: Tarkka-ampuja/Марксмен (RK-95)",
    "3. Panssarintorjunta-ampuja/Гранатометник (Rk-95\\M136)",
    "4: Partionjohtaja/Лідер групи (RK-95)",
    "5. Konekivääriampuja/Кулеметник (PKM)",
    "6: Kivääriampuja/Стрілець (RK-95)",
    "7: Tiiminjohtaja/Лідер групи (RK-95)",
    "8. Konekivääriampuja/Кулеметник (PKM)",
    "9: Taistelusairaanhoitaja/Медик (RK-95) | MED"
]

# Поточні зайнятість слотів
slot_users = [None] * len(slot_lines)

# Перевіряє, чи рядок починається з числа + "." або ":"
def is_slot(line: str) -> bool:
    text = line.strip()
    for n in range(1, 100):
        if text.startswith(f"{n}.") or text.startswith(f"{n}:"):
            return True
    return False

# View, що динамічно додає одну кнопку на кожен валідний слот
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        row = 0
        for idx, line in enumerate(slot_lines):
            if is_slot(line):
                self.add_item(SlotButton(idx, row))
                row += 1

# Кнопка, яка перемикає стан слота
class SlotButton(Button):
    def __init__(self, index: int, row: int):
        self.index = index
        label = "Вільний" if slot_users[index] is None else "Відмовитись"
        style = (
            discord.ButtonStyle.success
            if slot_users[index] is None
            else discord.ButtonStyle.danger
        )
        super().__init__(
            label=label,
            style=style,
            custom_id=f"slot_{index}",
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        # Якщо слот вільний — займаємо його
        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(
                f"✅ Ви записались у слот {self.index + 1}", ephemeral=True
            )
        # Якщо зайнятий саме ти — відмовляєшся
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(
                f"❌ Ви відмовились від слота {self.index + 1}", ephemeral=True
            )
        # Якщо зайнятий хтось інший
        else:
            await interaction.response.send_message(
                "⚠️ Цей слот зайнятий іншим користувачем", ephemeral=True
            )
            return

        # Після зміни — оновлюємо повідомлення з кнопками
        await interaction.message.edit(content=format_slots(), view=SlotView())

# Формує текстовий опис усіх валідних слотів
def format_slots() -> str:
    header = "**Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara**\n"
    body = ""
    for idx, line in enumerate(slot_lines):
        if is_slot(line):
            mention = slot_users[idx].mention if slot_users[idx] else ""
            body += f"{line}\n{mention}\n"
    return header + body

@bot.event
async def on_ready():
    print(f"[on_ready] Бот запущено @ {datetime.datetime.utcnow().isoformat()} UTC")
    # Повідомляємо в першому доступному каналі про перезапуск
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
    if message.author.bot:
        return

    # Лог для перевірки, що handler спрацьовує
    print(f"[on_message] {message.author}: {message.content}")

    # Якщо бачимо фразу "запис слоти" де завгодно в повідомленні
    if "запис слоти" in message.content.lower():
        global slot_users
        slot_users = [None] * len(slot_lines)
        ctx = await bot.get_context(message)
        await ctx.send(content=format_slots(), view=SlotView())

    await bot.process_commands(message)

# Команда для перевірки, у яких слотах ти записаний
@bot.command()
async def моїслоти(ctx: commands.Context):
    user = ctx.author
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == user and is_slot(slot_lines[i])
    ]
    msg = "🎯 Ви записані у:\n" + "\n".join(taken) if taken else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

# Статус бота
@bot.command()
async def статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=commit, inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)

# Виклик деплою на Render
@bot.command()
async def оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ DEPLOY_HOOK_URL не задано в налаштуваннях")
        return
    async with aiohttp.ClientSession() as sess:
        async with sess.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send("🔄 Оновлення викликано! Render запускає нову версію…")

# Інструкція Git Push
@bot.command()
async def gitpush(ctx: commands.Context):
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    embed.add_field(name="4. Push", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=embed)

# Старт бота
bot.run(TOKEN)