import os
import re
import subprocess
import aiohttp
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive та ENV ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 2. Регулярки та сесії ──────────────────────────────────────────────────────
TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"
sessions: dict[int, dict] = {}  # message_id → { title, lines, owners }
threads: dict[int, list[int]] = {}  # thread_id → list of message_ids

# ─── 3. Нагадування VTG ─────────────────────────────────────────────────────────
REMINDER_CHANNEL_ID = 1160843618433630228
KYIV_TZ = ZoneInfo("Europe/Kyiv")

@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if channel:
            try:
                await channel.send("||@everyone||\n**Сбор VTG**")
            except Exception as e:
                print(f"[vtg_reminder] Помилка надсилання: {e}")

# ─── 4. Embed ───────────────────────────────────────────────────────────────────
def build_embed(session: dict) -> discord.Embed:
    e = discord.Embed(title=session["title"], color=discord.Color.blue())
    lines = []
    for line, owner in zip(session["lines"], session["owners"]):
        if owner:
            lines.append(f"{line} – Зайнято {owner.mention}")
        else:
            lines.append(line)
    e.description = "\n".join(lines)
    return e

# ─── 5. Buttons ─────────────────────────────────────────────────────────────────
class SlotButton(Button):
    def __init__(self, session_id: int, idx: int, row: int):
        self.session_id = session_id
        self.idx = idx
        owner = sessions[session_id]["owners"][idx]
        label = f"{idx+1}. {'Зайняти' if owner is None else 'Відмовитись'}"
        style = discord.ButtonStyle.success if owner is None else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{session_id}-{idx}", row=row)

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.session_id]
        owner = sess["owners"][self.idx]
        thread_id = inter.message.channel.id
        thread_sessions = threads.get(thread_id, [])

        # Перевіряємо всі слоти в цій гілці
        for mid in thread_sessions:
            other = sessions[mid]
            if any(u == user for u in other["owners"] if u):
                if mid != self.session_id or other["owners"][self.idx] != user:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот у цьому відділенні (гілці).", ephemeral=True
                    )

        if owner is None:
            sess["owners"][self.idx] = user
        elif owner == user:
            sess["owners"][self.idx] = None
        else:
            return await inter.response.send_message(
                f"⚠️ Цей слот закріплено за {owner.mention}.", ephemeral=True
            )

        await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.session_id))

class SlotView(View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        count = len(sessions[session_id]["lines"])
        for idx in range(count):
            row = idx // 5
            self.add_item(SlotButton(session_id, idx, row))

# ─── 6. Events ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] Bot: {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    restart = discord.Embed(title="🔄 Бот перезапущено", description=f"📦 Commit: `{commit}`", color=discord.Color.green())
    for g in bot.guilds:
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages, g.text_channels)
        if ch:
            try: await ch.send(embed=restart)
            except: pass
    vtg_reminder.start()

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot: return
    lines = msg.content.splitlines()
    if any("запис слоти" in l.lower() for l in lines):
        header, slots, owners = None, [], []
        for raw in lines:
            txt = raw.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower(): continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = None
                for mention in msg.mentions:
                    if f"<@{mention.id}>" in txt or f"<@!{mention.id}>" in txt:
                        owner = mention
                        break
                clean = MENTION_RE.sub("", txt).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = txt

        slots = slots[:25]
        owners = owners[:len(slots)]
        session = { "title": header or DEFAULT_TITLE, "lines": slots, "owners": owners }
        embed = build_embed(session)
        sent = await msg.channel.send(embed=embed)
        sessions[sent.id] = session

        thread_id = msg.channel.id
        if thread_id not in threads:
            threads[thread_id] = []
        threads[thread_id].append(sent.id)

        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(msg)

# ─── 7. Service Commands ───────────────────────────────────────────────────────
@bot.command()
async def статус(ctx):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    emb = discord.Embed(title="🧠 Bot Status", color=discord.Color.blue())
    emb.add_field(name="Commit", value=commit, inline=True)
    emb.add_field(name="Token", value="✅" if TOKEN else "❌", inline=True)
    emb.add_field(name="Hook", value=DEPLOY_HOOK_URL or "None", inline=False)
    await ctx.send(embed=emb)

@bot.command()
async def оновити(ctx):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не задано")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Render-деплой тригерено!")

@bot.command()
async def gitpush(ctx):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stasd\\Downloads\\botslot`", inline=False)
    emb.add_field(name="2. git add", value="`git add .`", inline=False)
    emb.add_field(name="3. git commit", value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push", value="`git push origin main`", inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 8. Run ─────────────────────────────────────────────────────────────────────
bot.run(TOKEN)