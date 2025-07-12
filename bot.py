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
    "1. Example Slot One",
    "2. Example Slot Two",
    "3. Example Slot Three"
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
        print(f"[callback] user={user.name}, slot={self.index}")
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
    text = "**DEBUG TEST: Слоти**\n"
    for i, line in enumerate(slot_lines):
        user = slot_users[i].mention if slot_users[i] else ""
        text += f"{line}\n{user}\n"
    return text

@bot.event
async def on_ready():
    print("[on_ready] Бот запущено")
    print(f"[INTENTS] message_content: {intents.message_content}")
    print(f"[BOT] Guilds: {[g.name for g in bot.guilds]}")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            print(f"[CHANNEL] {channel.name} → Can send: {perms.send_messages}")
            if perms.send_messages:
                try:
                    commit = subprocess.getoutput("git rev-parse --short HEAD")
                    await channel.send(f"🟢 Бот запущено\n📦 Commit: `{commit}`")
                    break
                except Exception as e:
                    print(f"[on_ready] SEND ERROR: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    print(f"[on_message] Message received: {message.content}")
    if "запис слоти" in message.content.lower():
        try:
            global slot_users
            slot_users = [None] * len(slot_lines)
            ctx = await bot.get_context(message)
            await ctx.send(content=format_slots(), view=SlotView())
        except Exception as e:
            print(f"[on_message] ERROR: {e}")
    await bot.process_commands(message)

@bot.command()
async def debug(ctx):
    await ctx.send(f"🔍 message_content intent: `{bot.intents.message_content}`")

@bot.command()
async def статус(ctx):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(f"🧠 Commit: `{commit}`")

bot.run(TOKEN)