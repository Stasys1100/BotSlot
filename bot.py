import os
import subprocess
import aiohttp
import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

keep_alive()
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

slot_lines = [
    "1. Ryhmäjohtaja/Командир відділення (RK-95)",
    "2. Tarkka-ampuja/Марксмен (RK-95)",
    "3. Panssarintorjunta-ampuja/Гранатометник (Rk-95\\M136)",
    "4. Partionjohtaja/Лідер групи (RK-95)",
    "5. Konekivääriampuja/Кулеметник (PKM)",
    "6. Kivääriampuja/Стрілець (RK-95)",
    "7. Tiiminjohtaja/Лідер групи (RK-95)",
    "8. Konekivääriampuja/Кулеметник (PKM)",
    "9. Taistelusairaanhoitaja/Медик (RK-95) | MED"
]
slot_users = [None] * len(slot_lines)

class SlotView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(len(slot_lines)):
            self.add_item(SlotButton(i, row=i))

class SlotButton(Button):
    def __init__(self, index, row):
        self.index = index
        label = "Вільний" if slot_users[index] is None else "Відмовитись"
        style = discord.ButtonStyle.success if slot_users[index] is None else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot_{index}", row=row)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if slot_users[self.index] is None:
            slot_users[self.index] = user
            await interaction.response.send_message(f"✅ Ви записались у слот {self.index + 1}", ephemeral=True)
        elif slot_users[self.index] == user:
            slot_users[self.index] = None
            await interaction.response.send_message(f"❌ Ви відмовились від слота {self.index + 1}", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Слот зайнятий іншим", ephemeral=True)
            return

        await interaction.message.edit(content=format_slots(), view=SlotView())

def format_slots():
    text = "**Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara**\n"
    for i, line in enumerate(slot_lines):
        user = slot_users[i].mention if slot_users[i] else ""
        text += f"{line}\n{user}\n"
    return text

@bot.event
async def on_ready():
    print(f"✅ Bot started @ {datetime.datetime.now(datetime.UTC).isoformat()} UTC")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                try:
                    commit = subprocess.getoutput("git rev-parse --short HEAD")
                    await channel.send(f"🔄 Бот перезапущено\n📦 Commit: `{commit}`")
                    break
                except:
                    pass

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.strip().lower()
    if "запис слоти" in content:
        try:
            ctx = await bot.get_context(message)
            global slot_users
            slot_users = [None] * len(slot_lines)
            await ctx.send(content=format_slots(), view=SlotView())
        except Exception as e:
            print(f"[ERROR] on_message: {e}")

    await bot.process_commands(message)

@bot.command()
async def моїслоти(ctx):
    user = ctx.author
    rows = [slot_lines[i] for i, u in enumerate(slot_users) if u == user]
    msg = "🎯 Ви записані у:\n" + "\n".join(rows) if rows else "🕸 Ви не записані у жоден слот"
    await ctx.send(msg)

@bot.command()
async def статус(ctx):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    embed.add_field(name="Commit", value=commit, inline=True)
    embed.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    embed.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        await ctx.send("❌ DEPLOY_HOOK_URL не знайдено")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DEPLOY_HOOK_URL) as resp:
            await ctx.send("🔄 Оновлення викликано! Render запускає нову версію…")

@bot.command()
async def gitpush(ctx):
    embed = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    embed.add_field(name="1. Перейти в папку", value="`cd C:\Users\stasd\Downloads\botslot`", inline=False)
    embed.add_field(name="2. Додати файли", value="`git add .`", inline=False)
    embed.add_field(name="3. Коміт", value='`git commit -m "Оновлення слота"`', inline=False)
    embed.add_field(name="4. Push", value="`git push origin main`", inline=False)
    embed.set_footer(text="Після push → !оновити для Render-деплою")
    await ctx.send(embed=embed)

bot.run(TOKEN)