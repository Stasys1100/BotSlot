import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import os
from flask import Flask
import threading
from dotenv import load_dotenv
import datetime
import aiohttp

# Завантаження змінних із .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
PORT = int(os.getenv("PORT"))
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ☁️ Flask для keep-alive
app = Flask('')

@app.route('/')
def home():
    return "Bot is online"

def run():
    app.run(host='0.0.0.0', port=PORT)  # ✅ фіксований спосіб для Render

threading.Thread(target=run).start()

# 🤖 Discord-бот
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 📋 View-кнопки
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)

# 🚀 Запуск
@bot.event
async def on_ready():
    print(f"🔌 Bot ready @ {datetime.datetime.now()}")
    try:
        synced = await bot.tree.sync()
        print(f"📘 Synced {len(synced)} command(s)")
        channel = bot.get_channel(LOG_CHANNEL_ID)
        await channel.send(f"🚀 Бот стартував @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"❌ Помилка запуску: {e}")

# 📨 Повідомлення
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.content.lower() == "моїслоти":
        try:
            await message.delete()
        except:
            pass
        embed = discord.Embed(
            title="Ваші слоти 🎯",
            description="Текстова версія слотів",
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed, delete_after=30)
    await bot.process_commands(message)

# ⚙️ Слеш-команда /моїслоти
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def slash_slots(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це приватна слеш-команда",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 🧪 Команди з префіксом '!'
@bot.command()
async def тест(ctx):
    await ctx.send(f"HOOK: {DEPLOY_HOOK_URL}")

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Hook не знайдено")
        return
    await ctx.send("🔄 Відправляю запит на Render…")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DEPLOY_HOOK_URL) as resp:
                if resp.status == 200:
                    await ctx.send("✅ Оновлення запущено!")
                else:
                    await ctx.send(f"❌ Помилка: {resp.status}")
    except Exception as e:
        await ctx.send(f"💥 Виняток: {e}")

@bot.command()
async def перезапуск(ctx):
    await ctx.send("🔁 Бот перезапущено (умовно)")

@bot.command()
async def clear(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount)
    await ctx.send(f"🧹 Очищено {amount} повідомлень", delete_after=5)

@bot.command()
async def запис(ctx):
    embed = discord.Embed(
        title="Слоти 🔄",
        description="Оберіть дію нижче:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=SlotView())

# ▶️ Запуск бота
bot.run(TOKEN) 