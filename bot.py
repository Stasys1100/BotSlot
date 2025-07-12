import os
import subprocess
import aiohttp
import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# Запускаємо keep-alive та завантажуємо .env
keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# Інтенти для читання контенту повідомлень
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Довільний список слотів — можна добавляти/редагувати/видаляти рядки
slot_lines = [
    "1. Командир відділення (RK-95)",
    "2: Марксмен (RK-95)",
    "3. Гранатометник (Rk-95/M136)",
    "4: Лідер групи (RK-95)",
    "5. Кулеметник (PKM)",
    "6: Стрілець (RK-95)",
    "7. Лідер групи (RK-95)",
    "8. Кулеметник (PKM)",
    "9. Медик (RK-95)"
]
# Стан кожного слота (None — вільний, інакше — об’єкт User)
slot_users = [None] * len(slot_lines)


def is_slot(line: str) -> bool:
    """
    Перевіряє, чи рядок починається з “N.” або “N:”
    (N від 1 до 99). Якщо так — вважаємо це валідним слотом.
    """
    text = line.strip()
    for n in range(1, 100):
        if text.startswith(f"{n}.") or text.startswith(f"{n}:"):
            return True
    return False


class SlotView(View):
    """
    Динамічно будує кнопки для всіх валідних слотів,
    підраховуючи номер рядка для розміщення у стовпчик.
    """
    def __init__(self):
        super().__init__(timeout=None)
        row = 0
        for idx, line in enumerate(slot_lines):
            if is_slot(line):
                self.add_item(SlotButton(idx, row))
                row += 1


class SlotButton(Button):
    """
    Кнопка, що показує “N. Вільний” або “N. Відмовитись”
    і перемикає стан слота при натисканні.
    """
    def __init__(self, index: int, row: int):
        self.index = index
        free = slot_users[index] is None
        label = f"{index + 1}. {'Вільний' if free else 'Відмовитись'}"
        style = (discord.ButtonStyle.success if free
                 else discord.ButtonStyle.danger)
        super().__init__(label=label, style=style,
                         custom_id=f"slot_{index}", row=row)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        # Якщо вільний — займаємо
        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(
                f"✅ Ви записались у слот {self.index + 1}", ephemeral=True
            )
        # Якщо це ви — віддаємо назад
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(
                f"❌ Ви відмовились від слота {self.index + 1}", ephemeral=True
            )
        # Якщо зайнятий іншим — попереджаємо
        else:
            await interaction.response.send_message(
                "⚠️ Цей слот вже зайнятий іншим користувачем", ephemeral=True
            )
            return

        # Оновлюємо повідомлення із новим списком та кнопками
        await interaction.message.edit(content=format_slots(),
                                       view=SlotView())


def format_slots() -> str:
    """
    Формує текстовий блок із усіма валідними слотами
    і наявними під ними mentions.
    """
    header = "**Запис у слоти**\n"
    body = ""
    for idx, line in enumerate(slot_lines):
        if is_slot(line):
            mention = slot_users[idx].mention if slot_users[idx] else ""
            body += f"{line}\n{mention}\n"
    return header + body


@bot.event
async def on_ready():
    print(f"[on_ready] Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    # Повідомляємо в першому доступному каналі
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(
                        f"🔄 Бот перезапущено\n📦 Commit: `{commit}`"
                    )
                    return
                except:
                    continue


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(f"[on_message] {message.author}: {message.content}")

    # Реагуємо на будь-який варіант “запис слоти” (з крапками/двоеточчям/пробілами)
    if "запис слоти" in message.content.lower():
        global slot_users
        slot_users = [None] * len(slot_lines)
        ctx = await bot.get_context(message)
        await ctx.send(content=format_slots(), view=SlotView())

    await bot.process_commands(message)


@bot.command()
async def моїслоти(ctx: commands.Context):
    """Показує, в яких слотах ви вже записані."""
    user = ctx.author
    taken = [
        slot_lines[i]
        for i, u in enumerate(slot_users)
        if u == user and is_slot(slot_lines[i])
    ]
    msg = "🎯 Ви записані у:\n" + "\n".join(taken) \
          if taken else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)


@bot.command()
async def статус(ctx: commands.Context):
    """Відображає статус бота і поточний commit."""
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=commit, inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def оновити(ctx: commands.Context):
    """Тригерить новий деплой через Render webhook."""
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ DEPLOY_HOOK_URL не задано")
        return
    async with aiohttp.ClientSession() as sess:
        async with sess.post(DEPLOY_HOOK_URL):
            await ctx.send("🔄 Оновлення викликано! Render запускає нову версію…")


@bot.command()
async def gitpush(ctx: commands.Context):
    """Покрокова інструкція з git push → !оновити."""
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку",
                    value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт",
                    value='`git commit -m "Оновлення слота"`', inline=False)
    embed.add_field(name="4. Push", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=embed)


bot.run(TOKEN)