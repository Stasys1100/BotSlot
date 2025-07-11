import os
import re
import sys
import asyncio
import aiohttp
from datetime import datetime
from threading import Thread

import discord
from discord.ext import commands
from discord.ui import View, Button
from flask import Flask
from dotenv import load_dotenv

# --- Flask для HTTP Ping (якщо потрібен) ---
app = Flask("")
@app.route("/")
def home():
    return "Alive"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Якщо ти хочеш HTTP пінг, розкоментуй:
# Thread(target=run_flask, daemon=True).start()

# --- Load .env ---
load_dotenv()
TOKEN          = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))
PING_URL       = os.getenv("PING_URL")  # якщо потрібен пінг

intents = discord.Intents.default()
intents.message_content = True

# --- Bot та префікси ---
bot = commands.Bot(command_prefix="!", intents=intents)

# Зберігаємо слоти та глобальні дані
user_slots   = {}
global_embed = None
global_view  = None
global_msg   = None

_DIGIT_EMOJI = {str(i): f"{i}⃣" for i in range(1, 21)}
def num_e(n): return _DIGIT_EMOJI.get(str(n), f"{n}⃣")

# --- Кнопки для слотів ---
class SlotButton(Button):
    def __init__(self, idx, num, txt):
        super().__init__(label=f"{num_e(num)} Вільний", style=discord.ButtonStyle.green)
        self.idx, self.num, self.txt = idx, num, txt

    async def callback(self, interact):
        uid = interact.user.id
        v = global_view

        if uid in v.user_slot_map.values():
            return await interact.response.send_message("❌ У вас вже є слот", ephemeral=True)

        v.claimed[self.idx]       = uid
        v.user_slot_map[self.idx] = uid
        v.original_lines[self.idx] = self.txt
        user_slots.setdefault(uid, []).append((self.idx, self.txt, self.num))

        lines = v.embed.description.split("\n")
        lines[self.idx] = f"{self.txt} — Зайнято ({interact.user.mention})"
        v.embed.description = "\n".join(lines)
        v.update_buttons()
        await global_msg.edit(embed=v.embed, view=v)
        await interact.response.defer()

class SlotTaken(Button):
    def __init__(self, num):
        super().__init__(label=f"{num_e(num)} Зайнято", style=discord.ButtonStyle.gray, disabled=True)

class PaginatedSlotView(View):
    def __init__(self, embed, slots, original_lines):
        super().__init__(timeout=None)
        self.embed          = embed
        self.slots          = slots
        self.original_lines = original_lines
        self.claimed        = {}
        self.user_slot_map  = {}
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        for idx, num, txt in self.slots:
            if idx in self.claimed:
                self.add_item(SlotTaken(num))
            else:
                self.add_item(SlotButton(idx, num, txt))

class CancelPersonalButton(Button):
    def __init__(self, uid, idx, original, num):
        super().__init__( label=f"{num_e(num)} ❌ Відмовитись", style=discord.ButtonStyle.red )
        self.uid, self.idx, self.original, self.num = uid, idx, original, num

    async def callback(self, interact):
        if interact.user.id != self.uid:
            return await interact.response.send_message("❌ Це не ваш слот!", ephemeral=True)

        user_slots[self.uid] = [s for s in user_slots[self.uid] if s[0] != self.idx]
        v = global_view
        v.claimed.pop(self.idx, None)
        v.user_slot_map.pop(self.idx, None)

        lines = v.embed.description.split("\n")
        lines[self.idx] = self.original
        v.embed.description = "\n".join(lines)
        v.update_buttons()
        await global_msg.edit(embed=v.embed, view=v)
        await interact.response.send_message(f"✅ Слот №{self.num} звільнено", ephemeral=True)

# --- Команди ---
@bot.command(name="перезапуск")
async def restart_bot(ctx):
    await ctx.send("🔄 Перезапускаю бота...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@bot.command(name="clear")
async def clear_slots(ctx):
    user_slots.clear()
    await ctx.send("🗑️ Усі слоти очищені")

@bot.command(name="status")
async def status(ctx):
    desc = ""
    for uid, slots in user_slots.items():
        desc += f"<@{uid}>: {len(slots)} слот(ів)\n"
    if not desc:
        desc = "Немає зайнятих слотів"
    await ctx.send(f"📊 Статус слоту:\n{desc}")

@bot.command(name="моїслоти", aliases=["мійслоти","мійслот","мойслоти","мойслот"])
async def my_slots(ctx):
    uid = ctx.author.id
    slots = user_slots.get(uid, [])
    if not slots:
        return await ctx.send("🙅‍♂️ Ви не маєте жодного слоту")
    embed = discord.Embed(title="🗂 Ваші слоти", color=0x00AA00)
    view  = View(timeout=None)
    desc  = ""
    for i, (idx, txt, num) in enumerate(slots, 1):
        desc += f"{i}. {txt}\n"
        view.add_item(CancelPersonalButton(uid, idx, txt, num))
    embed.description = desc
    await ctx.send(embed=embed, view=view)

# --- Build slots з простим ключем без "!" ---
async def build_slots(message):
    global global_embed, global_view, global_msg
    if global_msg:
        await global_msg.delete()
    global_embed = None
    global_view  = None
    global_msg   = None

    lines = message.content.split("\n")[1:]
    blocks, curr = [], []
    for ln in lines:
        if not ln.strip():
            continue
        if not re.match(r"^\s*\d+[.:]", ln) and curr:
            blocks.append(curr)
            curr = []
        curr.append(ln)
    if curr:
        blocks.append(curr)

    all_lines, slots, seen = [], [], set()
    for bi, block in enumerate(blocks):
        for ln in block:
            all_lines.append(ln)
            m = re.match(r"^\s*(\d+)[.:]\s*(.*)", ln)
            if not m:
                continue
            num   = int(m.group(1))
            clean = re.sub(r"[–—-]\s*Зайнято.*$", "", ln).strip()
            key   = f"{bi}|{clean.lower()}"
            if "зайнято" in ln.lower() or key in seen:
                continue
            seen.add(key)
            slots.append((len(all_lines)-1, num, clean))

    global_embed = discord.Embed(title="📋 Слоти", description="\n".join(all_lines), color=0x00FF00)
    global_view  = PaginatedSlotView(global_embed, slots, all_lines)
    global_msg   = await message.channel.send(embed=global_embed, view=global_view)

# --- Події ---
@bot.event
async def on_ready():
    if not getattr(bot, "started", False):
        bot.started = True
        print(f"🔌 Bot ready @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        # Лог у Діскорд
        if LOG_CHANNEL_ID:
            try:
                ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
                await ch.send(f"🚀 Бот стартував @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            except:
                pass
        # Запускаємо пінг тільки раз
        if PING_URL:
            bot.loop.create_task(self_ping())

@bot.event
async def on_command_error(ctx, error):
    print(f"🔴 Command Error: {type(error).__name__}: {error}")
    if not isinstance(error, commands.CommandNotFound):
        await ctx.send(f"❌ Помилка: {error}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.strip()
    lower   = content.lower()

    # 1) Спеціальна без-префікс команда
    if lower.startswith("запис слоти"):
        return await build_slots(message)

    # 2) Інші команди без "!"
    prefixless = {
        "перезапуск":    restart_bot,
        "clear":         clear_slots,
        "status":        status,
        "моїслоти":      my_slots,
        "мійслоти":      my_slots,
        "мійслот":       my_slots,
        "мойслоти":      my_slots,
        "мойслот":       my_slots,
    }
    for key, func in prefixless.items():
        if lower.startswith(key):
            ctx = await bot.get_context(message)
            return await func(ctx)

    # 3) Стандартні команди з "!"
    await bot.process_commands(message)

# --- Auto-ping щоб тримати свій URL активним ---
async def self_ping():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(PING_URL)
                print(f"🔁 Ping OK @ {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"Ping failed: {e}")
        await asyncio.sleep(600)

# --- Старт бота ---
if __name__ == "__main__":
    bot.run(TOKEN)