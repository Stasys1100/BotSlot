import os
import subprocess
import aiohttp
import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# Інтент для читання тексту повідомлень
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Довільний список рядків – кнопки лише для тих, які починаються з N. або N:
slot_lines = [
    "1. Командир відділення (RK-95)",
    "2: Марксмен (RK-95)",
    "3. Гранатометник (Rk-95/M136)",
    "4: Лідер групи (RK-95)",
    "5. Кулеметник (PKM)",
    "6: Стрілець (RK-95)",
    "7: Лідер групи (RK-95)",
    "8: Кулеметник (PKM)",
    "9: Медик (RK-95)"
]
slot_users = [None] * len(slot_lines)

def is_slot(line: str) -> bool:
    text = line.strip()
    for n in range(1, 100):
        if text.startswith(f"{n}.") or text.startswith(f"{n}:"):
            return True
    return False

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
        super().__init__(label=label, style=style, custom_id=f"slot_{index}", row=row)
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
                "⚠️ Цей слот зайнятий іншим користувачем", ephemeral=True
            )
            return

        # Оновлюємо список і кнопки в повідомленні
        await interaction.message.edit(content=format_slots(), view=SlotView())

def format_slots() -> str:
    header = "**Запис у слоти**\n"
    body = ""
    for idx, line in enumerate(slot_lines):
        if is_slot(line):
            mention = slot_users[idx].mention if slot_users[idx] else ""
            body += f"{line}\n{mention}\n"
    return header + body

@bot.event
async def on_ready():
    print("[on_ready] Бот стартував")
    print(f"[on_ready] message_content intent = {bot.intents.message_content}")
    print(f"[on_ready] Guilds = {[g.name for g in bot.guilds]}")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            print(f"[on_ready] Канал {channel.name} → send_messages={perms.send_messages}")
            if perms.send_messages:
                commit = subprocess.getoutput("git rev-parse --short HEAD")
                try:
                    await channel.send(f"🔄 Бот перезапущено\n📦 Commit: `{commit}`")
                    return
                except Exception as e:
                    print(f"[on_ready] Помилка при відправці: {e}")

@bot.event
async def on_message(message: discord.Message):
    print(f"[on_message] отримано: {message.content}")
    if message.author.bot:
        return

    if "запис слоти" in message.content.lower():
        global slot_users
        slot_users = [None] * len(slot_lines)
        ctx = await bot.get_context(message)
        await ctx.send(content=format_slots(), view=SlotView())

    await bot.process_commands(message)

@bot.command()
async def debug(ctx: commands.Context):
    """
    Показує стан інтента та навколишню діагностику.
    """
    guilds = [g.name for g in bot.guilds]
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
        await ctx.send("❌ DEPLOY_HOOK_URL не задано")
        return
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

bot.run(TOKEN)