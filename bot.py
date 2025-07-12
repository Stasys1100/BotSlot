import os
import subprocess
import datetime
import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

MAX_SLOTS = 2
slot_users = [None] * MAX_SLOTS

# 🎯 View з кнопками “Вільний” ↔ “Відмовитись”
class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(MAX_SLOTS):
            self.add_item(SlotButton(i))

class SlotButton(Button):
    def __init__(self, index):
        self.index = index
        super().__init__(
            label="Вільний" if slot_users[index] is None else "Відмовитись",
            style=discord.ButtonStyle.success if slot_users[index] is None else discord.ButtonStyle.danger,
            custom_id=f"slot_{index}"
        )

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        global slot_users

        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(f"✅ Ви записались у слот {self.index + 1}", ephemeral=True)
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(f"❌ Ви відмовились від слота {self.index + 1}", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Цей слот вже зайнятий іншим", ephemeral=True)
            return

        text = "**Запис слоти**\n"
        for i in range(MAX_SLOTS):
            if slot_users[i]:
                text += f"{i + 1}. {slot_users[i].mention}\n"
            else:
                text += f"{i + 1}.\n"

        await interaction.message.edit(content=text, view=SlotView())

# 📦 Команда без префікса
@bot.command(name="запис_слоти")
async def запис_слоти(ctx):
    global slot_users
    slot_users = [None] * MAX_SLOTS
    text = "**Запис слоти**\n" + "\n".join([f"{i + 1}." for i in range(MAX_SLOTS)])
    await ctx.send(text, view=SlotView())

# 🧠 Коміт хеш
def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except:
        return "unknown"

# 🚀 Старт
@bot.event
async def on_ready():
    print(f"✅ Bot started @ {datetime.datetime.utcnow().isoformat()} UTC")
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send("🛰 Бот успішно запущено!")

# 🔄 Оновлення Render
@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ DEPLOY_HOOK_URL не знайдено")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send(f"🔔 Render: {resp.status}")

# 🔁 Перезапуск
@bot.command()
async def перезапустити(ctx):
    await ctx.send("🔁 Перезапуск виконано")

# 📊 Статус
@bot.command()
async def status(ctx):
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=get_commit_hash(), inline=True)
    embed.add_field(name="PORT", value=str(PORT), inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)

# 📥 Мої слоти
@bot.command()
async def моїслоти(ctx):
    user = ctx.author
    slots = [i + 1 for i, u in enumerate(slot_users) if u == user]
    if slots:
        msg = "🎯 Ви записані у слоти: " + ", ".join(map(str, slots))
    else:
        msg = "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

# 💾 Інструкція push
@bot.command()
async def gitpush(ctx):
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку", value="`cd botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт", value='`git commit -m "Оновлення"`', inline=False)
    embed.add_field(name="4. Пуш", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після цього — !оновити для деплою")
    await ctx.send(embed=embed)

bot.run(TOKEN)