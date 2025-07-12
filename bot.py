import os
import threading
import datetime
import subprocess
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from flask import Flask
from dotenv import load_dotenv

# 🔑 .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT = int(os.getenv("PORT") or 10000)
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# 🌐 Flask keep-alive
app = Flask("")
@app.route("/")
def home():
    return "Bot is online"
def run_flask():
    app.run(host="0.0.0.0", port=PORT)
threading.Thread(target=run_flask, daemon=True).start()

# 🤖 Discord bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 🧠 Commit hash
def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except:
        return "unknown"

# 💡 SlotView з пофікшеною кнопкою
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction, button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, button):
        try:
            await interaction.message.delete()
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Помилка видалення: {e}", ephemeral=True)
            return
        # 💬 Ефемерна реакція — видима лише натискачу
        await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)

# 🟢 on_ready
@bot.event
async def on_ready():
    commit = get_commit_hash()
    log = (
        f"🚀 Bot ready @ {datetime.datetime.utcnow().isoformat()} UTC\n"
        f"• Commit: `{commit}`\n"
        f"• PORT: {PORT}\n"
        f"• Hook: {DEPLOY_HOOK_URL[:32]}..."
    )
    print(log)
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send(f"🛰 STATUS:\n{log}")
    await bot.tree.sync()

# 📋 /status
@bot.tree.command(name="status", description="Показує стан бота")
async def status(interaction):
    embed = discord.Embed(title="🛰 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=get_commit_hash(), inline=True)
    embed.add_field(name="PORT", value=str(PORT), inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 📋 /моїслоти
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def slash_slots(interaction):
    embed = discord.Embed(title="Ваші слоти 🎯", description="Це приватна слеш-команда", color=discord.Color.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 💬 Обробка ! команд
@bot.event
async def on_message(message):
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
                await message.channel.send(f"🔔 Render: {resp.status}")
        return

    if txt == "!перезапустити":
        await message.channel.send("🔁 Перезапуск виконано (умовно)")
        return

    if txt == "!запис":
        embed = discord.Embed(title="Слоти 🔄", description="Оберіть дію нижче:", color=discord.Color.blue())
        await message.channel.send(embed=embed, view=SlotView())
        return

    if txt == "моїслоти":
        try: await message.delete()
        except: pass
        embed = discord.Embed(title="Ваші слоти 🎯", description="Текстова версія слотів", color=discord.Color.green())
        await message.channel.send(embed=embed, delete_after=30)
        return

    await bot.process_commands(message)

# ▶️ Запуск
bot.run(TOKEN)