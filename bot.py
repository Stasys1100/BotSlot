# bot.py
import os
import datetime
import subprocess
import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, button
from dotenv import load_dotenv
from keep_alive import keep_alive

# 🌐 Flask (активує keep-alive сервер)
keep_alive()

# 🧠 Environment
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# 🔐 Захист
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN не заданий у .env або Environment")

# 🤖 Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except:
        return "unknown"

# 🎯 Кнопки для слота
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Записатись", style=discord.ButtonStyle.success, custom_id="slot_signup")
    async def sign_up(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("✅ Ви записались!", ephemeral=True)

    @button(label="Відмовитись", style=discord.ButtonStyle.danger, custom_id="slot_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.message.delete()
            await interaction.response.send_message("❌ Слот видалено", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ Немає прав на видалення", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ Помилка: {exc}", ephemeral=True)

# 🚀 Бот стартував
@bot.event
async def on_ready():
    print(f"✅ Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"• PORT: {PORT}")
    print(f"• Commit: {get_commit_hash()}")
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send("🛰 Бот запущено успішно!")

# 📦 Команди
@bot.command()
async def запис(ctx):
    embed = discord.Embed(title="Слоти 🔄", description="Оберіть дію нижче:", color=discord.Color.blue())
    await ctx.send(embed=embed, view=SlotView())

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Хук не знайдено")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send(f"🔔 Render: {resp.status}")

@bot.command()
async def перезапустити(ctx):
    await ctx.send("🔁 Перезапуск виконано")

@bot.command()
async def моїслоти(ctx):
    embed = discord.Embed(title="Ваші слоти 🎯", description="Приватна версія", color=discord.Color.green())
    try:
        await ctx.author.send(embed=embed)
    except:
        await ctx.send("⚠️ Не вдалося надіслати у DM")

@bot.command()
async def status(ctx):
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=get_commit_hash(), inline=True)
    embed.add_field(name="PORT", value=str(PORT), inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    await ctx.send(embed=embed)

# ▶️ Запуск
bot.run(TOKEN)