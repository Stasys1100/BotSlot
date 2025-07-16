import os
import re
import subprocess
import aiohttp
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv
from keep_alive import keep_alive

# ─── 1. Keep-alive та ENV ───────────────────────────────────────────────────────
keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

# ─── 2. Інтенти та ініціалізація бота ───────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ           = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID    = 1160843618433630228
ADMIN_CHANNEL_ID  = 1395065909185478769

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}              # message_id → { title, lines, owners, channel_id }
claims: dict[tuple[int,int], list] = {}     # (message_id, idx) → [User, ...]
request_counter = 0                         # нумератор заявок

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haара"

# ─── 4. VTG-нагадувач ─────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except: pass

# ─── 5. Генератор Embed ─────────────────────────────────────────────────────────
def build_embed(sess: dict) -> discord.Embed:
    e = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    e.description = "\n".join(lines)
    return e

# ─── 6. SlotButton + SlotView ──────────────────────────────────────────────────
class SlotButton(Button):
    def __init__(self, sid:int, idx:int):
        owner = sessions[sid]["owners"][idx]
        free  = owner is None
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        ch_id = sess["channel_id"]

        # зайняти вільний слот
        if owner is None:
            for s in sessions.values():
                if s["channel_id"]==ch_id and user in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # звільнити свій слот
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # чужий слот → ефермерно “Претендувати”
        return await inter.response.send_message(
            f"⚠️ Слот зайнято {owner.mention}.",
            view=ClaimSlotView(self.sid, self.idx),
            ephemeral=True
        )

class SlotView(View):
    def __init__(self, sid:int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# ─── 7. Претендування на слот ─────────────────────────────────────────────────
class ClaimSlotButton(Button):
    def __init__(self, sid:int, idx:int):
        super().__init__("❗ Претендувати", discord.ButtonStyle.primary,
                         custom_id=f"claim-slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]

        for s in sessions.values():
            if s["channel_id"]==sess["channel_id"] and user in s["owners"]:
                return await inter.response.send_message(
                    "⚠️ Ви вже маєте слот у цій гілці.", ephemeral=True
                )

        key = (self.sid, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message(
                "ℹ️ Вже подали заявку.", ephemeral=True
            )
        lst.append(user)
        await inter.response.send_message("✅ Заявка прийнята.", ephemeral=True)

        global request_counter
        request_counter += 1
        embed = discord.Embed(
            title=f"📝 Заявка #{request_counter}",
            description=sess["title"], color=discord.Color.orange()
        )
        embed.add_field("Слот #", str(self.idx+1), inline=True)
        embed.add_field("Власник", 
            sess["owners"][self.idx].mention if sess["owners"][self.idx] else "Вільний", inline=True)
        embed.add_field("Кандидат", user.mention, inline=False)

        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            msg = await admin_ch.send(embed=embed)
            await msg.edit(view=ClaimDecisionView(self.sid, self.idx, user.id, msg.id))

class ClaimSlotView(View):
    def __init__(self, sid:int, idx:int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(sid, idx))

# ─── 8. Modal для рішення ──────────────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(self, sid:int, idx:int, uid:int, admin_msg_id:int, accept:bool):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.sid, self.idx          = sid, idx
        self.uid, self.admin_msg_id = uid, admin_msg_id
        self.accept                 = accept
        self.reason                 = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        key  = (self.sid, self.idx)
        claimant = await bot.fetch_user(self.uid)
        old_owner= sess["owners"][self.idx]
        reason   = self.reason.value

        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)
        else:
            lst = claims.get(key, [])
            if claimant in lst: lst.remove(claimant)

        # оновити головний Embed
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass

        # DM повідомлення
        try:
            if self.accept:
                await claimant.send(f"✅ Вас призначено на слот #{self.idx+1}.\nПричина: {reason}")
                if old_owner and old_owner!=claimant:
                    await old_owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\nПричина: {reason}"
                    )
            else:
                await claimant.send(f"❌ Ваша заявка на слот #{self.idx+1} відхилена.\nПричина: {reason}")
        except: pass

        # видалити адмін-повідомлення
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            try:
                adm = await admin_ch.fetch_message(self.admin_msg_id)
                await adm.delete()
            except: pass

        await inter.response.send_message("✔️ Готово.", ephemeral=True)

class ClaimDecisionButton(Button):
    def __init__(self, sid:int, idx:int, uid:int, adm_id:int, accept:bool):
        lbl  = "✅ Призначити" if accept else "❌ Відхилити"
        sty  = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag  = "accept" if accept else "deny"
        super().__init__(label=lbl, style=sty,
                         custom_id=f"dec-{tag}-{sid}-{idx}-{uid}-{adm_id}")
        self.sid, self.idx, self.uid, self.adm_id, self.accept = sid, idx, uid, adm_id, accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(self.sid, self.idx, self.uid, self.adm_id, self.accept)
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(self, sid:int, idx:int, uid:int, adm_id:int):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, uid, adm_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, uid, adm_id, False))

# ─── 9. Події ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    restart_embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    for g in bot.guilds:
        ch = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
                      and c.permissions_for(g.me).send_messages,
            g.text_channels
        )
        if ch:
            try: await ch.send(embed=restart_embed)
            except: pass
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return
    if "запис слоти" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            t = line.strip()
            if not t or "запис слоти" in t.lower() or "everyone" in t.lower():
                continue
            m = TRIGGER_RE.match(t)
            if m:
                owner = next(
                    (u for u in message.mentions
                     if f"<@{u.id}>" in t or f"<@!{u.id}>" in t),
                    None
                )
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = t

        slots, owners = slots[:25], owners[:len(slots)]
        sess = {
            "title":      header or DEFAULT_TITLE,
            "lines":      slots,
            "owners":     owners,
            "channel_id": message.channel.id
        }
        embed = build_embed(sess)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── 10. Команди ────────────────────────────────────────────────────────────────
@bot.command(name="оновити", aliases=["update"])
async def _оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановлено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Рендер-деплой тригерено!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(f"🧠 Commit: `{commit}`\n📊 Sessions: {len(sessions)}\n📋 Claims: {sum(len(v) for v in claims.values())}")

@bot.command(name="gitpush")
async def _gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field("1. cd до папки", "`cd C:\\Users\\stas\\botslot`", False)
    emb.add_field("2. git add", "`git add .`", False)
    emb.add_field("3. git commit", '`git commit -m "Оновлення слота"`', False)
    emb.add_field("4. git push", "`git push origin main`", False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 11. Адмін: зняти користувача зі слоту ───────────────────────────────────────
@bot.command(name="зняти")
async def зняти(
    ctx: commands.Context,
    session_msg_id: int,
    slot_number: int,
    member: discord.Member,
    *,
    reason: str = None
):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")

    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")

    idx = slot_number - 1
    if idx < 0 or idx >= len(session["owners"]):
        return await ctx.send("❌ Невірний номер слоту.")

    if session["owners"][idx] != member:
        return await ctx.send(f"⚠️ {member.mention} не є власником слоту #{slot_number}.")

    # знімаємо
    session["owners"][idx] = None

    # оновлюємо основне повідомлення
    orig_ch = bot.get_channel(session["channel_id"])
    try:
        orig_msg = await orig_ch.fetch_message(session_msg_id)
        await orig_msg.edit(embed=build_embed(session), view=SlotView(session_msg_id))
    except:
        pass

    # DM користувачу
    text = f"❗ Ви зняті зі слоту #{slot_number} у «{session['title']}»."
    if reason:
        text += f"\nПричина: {reason}"
    try:
        await member.send(text)
    except:
        pass

    await ctx.send(f"✅ {member.mention} знято зі слоту #{slot_number}.")    

# ─── 12. Старт бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)