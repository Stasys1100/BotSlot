import os
import threading
import datetime
import subprocess
import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, Button
from flask import Flask
from dotenv import load_dotenv

# 🧠 Завантаження .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))
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

# 🔍 Коміт хеш
def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except:
        return "unknown"

# 🎯 Кнопки для слота
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction, button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, button):
        try:
            await interaction.message.delete()
            await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Помилка: {e}", ephemeral=True)

# 🚀 on_ready
@bot.event
async def on_ready():
    print(f"🔌 Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"• PORT: {PORT}")
    print(f"• Hook: {DEPLOY_HOOK_URL}")
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send("🛰 Бот активний та готовий до роботи.")

# 🧪 !status — перевірка
@bot.command()
async def status(ctx):
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=get_commit_hash(), inline=True)
    embed.add_field(name="Port", value=str(PORT), inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    await ctx.send(embed=embed)

# 🔁 !оновити — деплой
@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Hook не знайдено")
        return
    await ctx.send("🔄 Відправляю запит до Render…")
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send(f"🔔 Render: {resp.status}")

# 🎯 !запис — слоти
@bot.command()
async def запис(ctx):
    embed = discord.Embed(
        title="Слоти 🔄",
        description="Оберіть дію нижче:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=SlotView())

# 🔁 !перезапустити
@bot.command()
async def перезапустити(ctx):
    await ctx.send("🔁 Перезапуск (умовний) виконано")

# 🧼 !моїслоти — видалення + приватне повідомлення
@bot.command()
async def моїслоти(ctx):
    try:
        await ctx.message.delete()
    except:
        pass
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це ваша персональна версія",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed, delete_after=30)

# ▶️ Запуск
bot.run(TOKEN)