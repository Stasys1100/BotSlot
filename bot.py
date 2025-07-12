import os
import subprocess
import datetime
import threading
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from flask import Flask
from dotenv import load_dotenv

# --------------------------------------------------------------------
#  Завантаження змінних середовища з .env
# --------------------------------------------------------------------
load_dotenv()
TOKEN            = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID   = os.getenv("LOG_CHANNEL_ID")
PORT             = os.getenv("PORT")
DEPLOY_HOOK_URL  = os.getenv("DEPLOY_HOOK_URL")

# --------------------------------------------------------------------
#  Простий Flask-сервер для keep-alive на Render
# --------------------------------------------------------------------
app = Flask("")

@app.route("/")
def home():
    return "Bot is online"

def run_flask():
    app.run(host="0.0.0.0", port=int(PORT or 0))

threading.Thread(target=run_flask, daemon=True).start()

# --------------------------------------------------------------------
#  Discord-бот із префіксом '!'
# --------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------------------------
#  Допоміжна функція: поточний git-коміт для діагностики
# --------------------------------------------------------------------
def get_commit_hash():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"]
        ).decode().strip()
    except:
        return "unknown"

# --------------------------------------------------------------------
#  View-кнопки для слоту
# --------------------------------------------------------------------
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.message.delete()
            await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Помилка видалення: {e}", ephemeral=True)

# --------------------------------------------------------------------
#  Подія on_ready: синхронізація слеш-команд + лог у канал
# --------------------------------------------------------------------
@bot.event
async def on_ready():
    commit = get_commit_hash()
    status_report = (
        f"🚀 Bot started @ {datetime.datetime.utcnow().isoformat()} UTC\n"
        f"• Commit: `{commit}`\n"
        f"• TOKEN set? {'Yes' if TOKEN else 'No'}\n"
        f"• LOG_CHANNEL_ID: {LOG_CHANNEL_ID}\n"
        f"• PORT: {PORT}\n"
        f"• DEPLOY_HOOK_URL: {DEPLOY_HOOK_URL[:30]}…\n"
    )
    print(status_report)

    # Відправка в канал логів, якщо задано
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send(f"🛰 STATUS:\n{status_report}")

    # Синхронізуємо слеш-команди
    await bot.tree.sync()

# --------------------------------------------------------------------
#  Слеш-команда /status для діагностики
# --------------------------------------------------------------------
@bot.tree.command(name="status", description="Показує стан окруження та версію коду")
async def cmd_status(interaction: discord.Interaction):
    embed = discord.Embed(title="🛰 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit",          value=get_commit_hash(), inline=False)
    embed.add_field(name="TOKEN set?",      value=str(bool(TOKEN)), inline=True)
    embed.add_field(name="LOG_CHANNEL_ID",  value=LOG_CHANNEL_ID or "None", inline=True)
    embed.add_field(name="PORT",            value=PORT or "None", inline=True)
    embed.add_field(name="DEPLOY_HOOK_URL", value=DEPLOY_HOOK_URL or "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --------------------------------------------------------------------
#  Слеш-команда /моїслоти
# --------------------------------------------------------------------
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def slash_slots(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це приватна слеш-команда",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --------------------------------------------------------------------
#  Обробка текстових команд через on_message
# --------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    txt = message.content.lower().strip()

    if txt == "!тест":
        await message.channel.send(f"HOOK: {DEPLOY_HOOK_URL}")
        return

    if txt == "!оновити":
        if not DEPLOY_HOOK_URL:
            await message.channel.send("❌ Hook не знайдено")
            return
        await message.channel.send("🔄 Відправляю запит на Render…")
        async with aiohttp.ClientSession() as session:
            async with session.post(DEPLOY_HOOK_URL) as resp:
                await message.channel.send(f"🔔 Render responded: {resp.status}")
        return

    if txt == "!перезапустити":
        await message.channel.send("🔁 Перезапуск виконано (умовно)")
        return

    if txt == "!запис":
        embed = discord.Embed(
            title="Слоти 🔄",
            description="Оберіть дію нижче:",
            color=discord.Color.blue()
        )
        await message.channel.send(embed=embed, view=SlotView())
        return

    if txt == "моїслоти":
        try:
            await message.delete()
        except:
            pass
        embed = discord.Embed(
            title="Ваші слоти 🎯",
            description="Текстова версія слотів (зникає через 30с)",
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed, delete_after=30)
        return

    # Пропуск до інших @bot.command(), якщо треба
    await bot.process_commands(message)

# --------------------------------------------------------------------
#  Запуск бота
# --------------------------------------------------------------------
bot.run(TOKEN)