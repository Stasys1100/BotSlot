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

# Flask для keep-alive
app = Flask('')

@app.route('/')
def home():
    return "Bot is online"

def run():
    app.run(host='0.0.0.0', port=PORT)

threading.Thread(target=run).start()

# Ініціалізація бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Клас для кнопок слотів
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Відповідь та видалення повідомлення з іконкою
        try:
            await interaction.message.delete()
            await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)
        except Exception as e:
            # можливо, повідомлення вже видалено
            await interaction.response.send_message(f"⚠️ Помилка: {e}", ephemeral=True)

# Обробка подій при запуску (ready)
@bot.event
async def on_ready():
    print(f"🔌 Bot ready @ {datetime.datetime.now()}")
    try:
        synced = await bot.tree.sync()
        print(f"📘 Synced {len(synced)} command(s)")
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(f"🚀 Бот стартував @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"❌ Стартова помилка: {e}")

# Основна обробка повідомлень
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    msg = message.content.lower()

    # Тест команда
    if msg == "!тест":
        await message.channel.send(f"HOOK: {DEPLOY_HOOK_URL}")
        return

    # Оновлення
    if msg == "!оновити":
        if not DEPLOY_HOOK_URL:
            await message.channel.send("❌ Hook не знайдено")
            return
        await message.channel.send("🔄 Відправляю запит на Render…")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(DEPLOY_HOOK_URL) as resp:
                    if resp.status == 200:
                        await message.channel.send("✅ Оновлення запущено!")
                    else:
                        await message.channel.send(f"❌ Помилка: {resp.status}")
        except Exception as e:
            await message.channel.send(f"💥 Виняток: {e}")
        return

    # Перезапуск
    if msg == "!перезапустити":
        await message.channel.send("🔁 Перезапуск виконано (умовно)")
        # Перезапуск у реальному часі - його потрібно робити зовні, або через зовнішні скрипти
        # Але тут залишається для прикладу тільки повідомлення
        return

    # Команда для натискання слотів
    if msg == "!запис":
        embed = discord.Embed(
            title="Слоти 🔄",
            description="Оберіть дію нижче:",
            color=discord.Color.blue()
        )
        await message.channel.send(embed=embed, view=SlotView())
        return

    # Мої слоти (звичайний меседж)
    if msg == "моїслоти":
        try:
            await message.delete()
        except:
            pass
        embed = discord.Embed(
            title="Ваші слоти 🎯",
            description="Це текстова версія слотів",
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed, delete_after=30)
        return

    await bot.process_commands(message)

# Команда /моїслоти (слеш команда)
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def slash_slots(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це приватна слеш-команда",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Підтвердження перезапуску або інша логіка тут можлива (якщо потрібно)

# Запуск
bot.run(TOKEN)