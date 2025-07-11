import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import os
from flask import Flask
import threading
from dotenv import load_dotenv
import datetime

# 🔧 Змінні середовища
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
PING_URL = os.getenv("PING_URL")

# 🌐 Flask для keep-alive
app = Flask('')

@app.route('/')
def home():
    return "Bot is online"

def run():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run).start()

# 🤖 Налаштування бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents)

# 🔄 Слоти з кнопками
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)

# 📣 Логування запуску
@bot.event
async def on_ready():
    print(f"🔌 Bot ready @ {datetime.datetime.now()}")
    try:
        await bot.tree.sync()
        channel = bot.get_channel(LOG_CHANNEL_ID)
        await channel.send(f"🚀 Бот стартував @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"Помилка при синхронізації: {e}")

# 🗑️ Видалення запиту "моїслоти"
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.content.lower() == "моїслоти":
        try:
            await message.delete()
        except discord.errors.Forbidden:
            pass

        embed = discord.Embed(
            title="Ваші слоти 🎯",
            description="Ось список ваших слотів! (приклад)",
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed, delete_after=30)

    await bot.process_commands(message)

# 🧩 Команда "перезапуск"
@bot.command()
async def перезапуск(ctx):
    await ctx.send("🔁 Бот перезапущено (умовно)")

# 🧹 Команда "clear"
@bot.command()
async def clear(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount)
    await ctx.send(f"🧹 Очищено {amount} повідомлень", delete_after=5)

# 📥 Команда "запис слоти" — з кнопками
@bot.command()
async def запис(ctx):
    embed = discord.Embed(
        title="Слоти 🔄",
        description="Оберіть дію нижче:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=SlotView())

# 🔐 Слеш-команда "моїслоти" — тільки для автора
@bot.tree.command(name="моїслоти", description="Показати свої слоти")
async def slot_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це ваш приватний список слотів!",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ▶️ Запуск
bot.run(TOKEN)