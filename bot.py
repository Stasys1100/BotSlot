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

slot_titles = [
    "1. Командир відділення",
    "2. Марксмен",
    "3. Гранатометник",
    "4. Лідер групи",
    "5. Кулеметник",
    "6. Стрілець",
    "7. Лідер групи (2)",
    "8. Кулеметник (2)",
    "9. Медик",
    "10. Інженер",
    "11. Сапер",
    "12. Радист",
    "13. Підсилення 1",
    "14. Підсилення 2",
    "15. Резерв"
]
slot_users = [None] * len(slot_titles)

class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(len(slot_titles)):
            row = i // 5
            self.add_item(SlotButton(i, row))

class SlotButton(Button):
    def __init__(self, index, row):
        self.index = index
        label = "Вільний" if slot_users[index] is None else "Відмовитись"
        style = discord.ButtonStyle.success if slot_users[index] is None else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot_{index}", row=row)

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
            await interaction.response.send_message("⚠️ Слот зайнятий іншим", ephemeral=True)
            return

        text = "**Запис слоти**\nAlpha 1-2 | Jalkaväen haara | Karhu\n"
        for i, title in enumerate(slot_titles):
            suffix = slot_users[i].mention if slot_users[i] else ""
            text += f"{title}\n{suffix}\n"

        await interaction.message.edit(content=text, view=SlotView())

@bot.command(name="запис_слоти")
async def запис_слоти(ctx):
    global slot_users
    slot_users = [None] * len(slot_titles)
    text = "**Запис слоти**\nAlpha 1-2 | Jalkaväen haara | Karhu\n"
    for title in slot_titles:
        text += title + "\n\n"
    await ctx.send(text, view=SlotView())

def get_commit_hash():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except:
        return "unknown"

@bot.event
async def on_ready():
    print(f"✅ Bot started @ {datetime.datetime.now(datetime.UTC).isoformat()} UTC")
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(LOG_CHANNEL_ID))
        if chan:
            await chan.send("🛰 Бот успішно запущено!")

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ DEPLOY_HOOK_URL не знайдено")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send(f"🔔 Render: {resp.status}")

@bot.command()
async def перезапустити(ctx):
    await ctx.send("🔁 Перезапуск виконано")

@bot.command()
async def status(ctx):
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=get_commit_hash(), inline=True)
    embed.add_field(name="PORT", value=str(PORT), inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def моїслоти(ctx):
    user = ctx.author
    slots = [f"{i + 1}. {slot_titles[i]}" for i, u in enumerate(slot_users) if u == user]
    msg = "🎯 Ви записані у:\n" + "\n".join(slots) if slots else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

@bot.command()
async def gitpush(ctx):
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    embed.add_field(name="4. Push на GitHub", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=embed)

bot.run(TOKEN)