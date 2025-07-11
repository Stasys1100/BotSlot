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

# Завантаження змінних
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
PING_URL = os.getenv("PING_URL")
PORT = int(os.getenv("PORT"))
DEPLOY_HOOK_URL = "https://api.render.com/deploy/srv-d1op6fk9c44c73ft182g?key=K_iwnXTdqgc"

# Flask для keep-alive
app = Flask('')

@app.route('/')
def home():
    return "Bot is online"

def run():
    app.run(host='0.0.0.0', port=PORT)

threading.Thread(target=run).start()

# Налаштування бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents)

# Кнопки слотів
class SlotView(View):
    @discord.ui.button(label="Записатись", style=discord.ButtonStyle.success)
    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @discord.ui.button(label="Відмовитись", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("❌ Ви відмовились від слота", ephemeral=True)

# Старт і логування
@bot.event
async def on_ready():
    print(f"🔌 Bot ready @ {datetime.datetime.now()}")
    try:
        await bot.tree.sync()
        channel = bot.get_channel(LOG_CHANNEL_ID)
        await channel.send(f"🚀 Бот стартував @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"❌ Помилка при запуску: {e}")

# Обробка повідомлень
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

# Слеш-команда "моїслоти"
@bot.tree.command(name="моїслоти", description="Показати свої слоти приватно")
async def my_slots(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ваші слоти 🎯",
        description="Це ваш приватний список слотів!",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Команда "перезапуск"
@bot.command()
async def перезапуск(ctx):
    await ctx.send("🔁 Бот перезапущено (умовно)")

# Команда "clear"
@bot.command()
async def clear(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount)
    await ctx.send(f"🧹 Очищено {amount} повідомлень", delete_after=5)

# Команда "запис"
@bot.command()
async def запис(ctx):
    embed = discord.Embed(
        title="Слоти 🔄",
        description="Оберіть дію нижче:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=SlotView())

# Команда "оновити" з логом
@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Deploy Hook не знайдено. Змінна DEPLOY_HOOK_URL = None")
        print("⛔ [оновити] Не знайдено DEPLOY_HOOK_URL. Оновлення не запущено.")
        return

    await ctx.send("🔄 Запускаю оновлення…")
    print(f"📡 [оновити] Надсилаю запит до: {DEPLOY_HOOK_URL}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DEPLOY_HOOK_URL) as resp:
                print(f"📬 [оновити] Статус відповіді Render: {resp.status}")

                if resp.status == 200:
                    await ctx.send("✅ Оновлення запущено! Render виконає деплой за кілька секунд.")
                    print("✅ [оновити] Оновлення успішно запущено.")
                else:
                    await ctx.send(f"❌ Помилка при оновленні. Код відповіді: {resp.status}")
                    print(f"⛔ [оновити] Відповідь не 200 OK → {resp.status}")
    except Exception as e:
        await ctx.send(f"❌ Запит не вдалося виконати: {e}")
        print(f"💥 [оновити] Виникла помилка: {e}")

# Запуск
bot.run(TOKEN)