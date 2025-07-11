\import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import os
from flask import Flask
import threading
from dotenv import load_dotenv
import datetime
import aiohttp

# 🔐 Завантаження змінних
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
PORT = int(os.getenv("PORT"))
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")  # Render hook для оновлення

# ☁️ Flask для keep-alive
app = Flask('')

@app.route('/')
def home():
    return "Bot is online"

def run():
    app.run(host='0.0.0.0', port=PORT)  # 🔧 критично — використовує PORT з .env

threading.Thread(target=run).start()

# 🤖 Discord bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents)

# 🎯 View-кнопки для слотів
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)

# 🚀 Старт бота
@bot.event
async def on_ready():
    print(f"🔌 Bot ready @ {datetime.datetime.now()}")
    try:
        await bot.tree.sync()
        channel = bot.get_channel(LOG_CHANNEL_ID)
        await channel.send(f"🚀 Бот стартував @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"❌ Помилка при запуску: {e}")

# 🧹 Автоочищення `моїслоти`
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
            description="Ось ваш список слотів! (приклад)",
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed, delete_after=30)

    await bot.process_commands(message)

# ✳️ Слеш-команда `/моїслоти`
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def my_slots(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це ваш приватний список слотів!",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 🔁 Команда `перезапуск`
@bot.command()
async def перезапуск(ctx):
    await ctx.send("🔁 Бот перезапущено (умовно)")

# 🧽 Команда `clear`
@bot.command()
async def clear(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount)
    await ctx.send(f"🧹 Очищено {amount} повідомлень", delete_after=5)

# 📅 Команда `запис`
@bot.command()
async def запис(ctx):
    embed = discord.Embed(
        title="Слоти 🔄",
        description="Оберіть дію нижче:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=SlotView())

# 🧪 Тестова команда — перевірка hook
@bot.command()
async def тест(ctx):
    await ctx.send(f"HOOK: {DEPLOY_HOOK_URL}")

# 🔄 Оновлення бота через Render hook
@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Deploy Hook не знайдено. Змінна DEPLOY_HOOK_URL = None")
        print("⛔ [оновити] DEPLOY_HOOK_URL = None")
        return

    await ctx.send("🔄 Запускаю оновлення…")
    print(f"📡 [оновити] Надсилаю запит до: {DEPLOY_HOOK_URL}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DEPLOY_HOOK_URL) as resp:
                print(f"📬 [оновити] Статус відповіді: {resp.status}")
                if resp.status == 200:
                    await ctx.send("✅ Оновлення запущено! Бот оновиться за кілька секунд.")
                    print("✅ [оновити] Успішно.")
                else:
                    await ctx.send(f"❌ Помилка оновлення. Код: {resp.status}")
                    print(f"⛔ [оновити] Код не 200 → {resp.status}")
    except Exception as e:
        await ctx.send(f"❌ Помилка при запиті: {e}")
        print(f"💥 [оновити] Виняток: {e}")

# ▶️ Запуск
bot.run(TOKEN)