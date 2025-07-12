import os
import datetime
import subprocess
import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# 🌐 Render Keep-Alive
keep_alive()

# 🌱 Environment
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# 🔐 Безпечний запуск
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN не знайдено в .env або Environment")

# 🤖 Bot без префікса
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents, help_command=None)

def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except Exception:
        return "unknown"

# 🎯 Slot View з toggle кнопками
class SlotView(View):
    def __init__(self, signed_up: bool = False):
        super().__init__(timeout=None)
        self.signed_up = signed_up
        label = "Відмовитись" if signed_up else "Записатись"
        style = discord.ButtonStyle.danger if signed_up else discord.ButtonStyle.success
        self.add_item(Button(label=label, style=style, custom_id="slot_toggle"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        new_view = SlotView(not self.signed_up)
        await interaction.message.edit(view=new_view)
        response = "✅ Ви записались!" if not self.signed_up else "❌ Ви відмовились!"
        await interaction.response.send_message(response, ephemeral=True)
        return False

# 🚀 Подія старту
@bot.event
async def on_ready():
    print(f"✅ Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    print(f"• PORT: {PORT}")
    print(f"• Commit: {get_commit_hash()}")
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send("🛰 Бот запущено успішно!")

# 📦 Команда слоту без "!"
@bot.command(name="запис_слоти")
async def запис_слоти(ctx):
    await ctx.send("**Запис слоти**\n1.\n2.", view=SlotView())

# 🔁 Оновлення Render
@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ Хук не знайдено")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send(f"🔔 Render: {resp.status}")

# 🔄 Перезапуск
@bot.command()
async def перезапустити(ctx):
    await ctx.send("🔁 Перезапуск виконано")

# 👤 Приватні слоти
@bot.command()
async def моїслоти(ctx):
    embed = discord.Embed(title="Ваші слоти 🎯", description="Приватна версія", color=discord.Color.green())
    try:
        await ctx.author.send(embed=embed)
    except:
        await ctx.send("⚠️ Не вдалося надіслати у DM")

# 📊 Статус
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