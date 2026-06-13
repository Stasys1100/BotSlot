import os
import re
import subprocess
import aiohttp
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput, Select
from discord import SelectOption
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
KYIV_TZ          = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID   = 1160843618433630228
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))

processed_messages: set[int] = set()
# sessions: message_id → { title, lines, owners, channel_id, forbidden }
sessions: dict[int, dict] = {}            
claims: dict[tuple[int,int], list] = {}   # (message_id, idx) → [User, ...]
request_counter = 0                       # лічильник заявок

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "3. Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. Щотижневий нагадувач VTG ────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try:
                await ch.send("||@everyone||\n**Сбор VTG**")
            except:
                pass

# ─── 5. Генератор Embed для слотів ─────────────────────────────────────────────
SIDE_COLORS = {
    "west":        discord.Color.from_rgb(41, 128, 185),   # синій
    "east":        discord.Color.from_rgb(192, 57, 43),    # червоний
    "independent": discord.Color.from_rgb(39, 174, 96),    # зелений
    "civilian":    discord.Color.from_rgb(127, 140, 141),  # сірий
}

def build_embed(sess: dict) -> discord.Embed:
    side = sess.get("side", "west")
    color = SIDE_COLORS.get(side, discord.Color.from_rgb(41, 128, 185))
    embed = discord.Embed(title=f"**{sess['title']}**", color=color)

    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        num = f"`{i+1:02d}`"
        slot_text = text.strip()
        if owner:
            lines.append(f"{num} {slot_text}\n┗ 👤 {owner.mention}")
        else:
            lines.append(f"{num} {slot_text}")

    embed.description = "\n".join(lines)

    total = len(sess["lines"])
    taken = sum(1 for o in sess["owners"] if o is not None)
    free  = total - taken
    embed.set_footer(text=f"Слоти: {taken}/{total} зайнято  •  Вільно: {free}")
    return embed

# ─── 6. SlotButton та SlotView ─────────────────────────────────────────────────
class SlotButton(Button):
    def __init__(self, sid: int, idx: int):
        owner = sessions.get(sid, {}).get("owners", [None])[idx]
        free = owner is None
       
        label = f"{idx+1}. {'Зайняти' if free else 'Відмовитись'}"
        style = discord.ButtonStyle.success if free else discord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        ch_id = sess["channel_id"]

        # ПЕРЕВІРКА НА ЗАБОРОНУ (для звичайних користувачів)
        forbidden_ids = sess.get("forbidden", [])[self.idx]
        if user.id in forbidden_ids:
            return await inter.response.send_message(
                "⛔ Цей слот заборонено для вас.", ephemeral=True
            )

        # 6.1) Вільний слот → зайняти
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # 6.2) Свій слот → звільнити
        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # 6.3) Чужий слот → пропонуємо претендувати
        return await inter.response.send_message(
            f"⚠️ Цей слот зайнято {owner.mention}.",
            view=ClaimSlotView(self.sid, self.idx),
            ephemeral=True
        )

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(SlotButton(sid, idx))

# ─── 7. “Претендувати” на слот ─────────────────────────────────────────────────
class ClaimSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(
            label="❗ Претендувати",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim-slot-{sid}-{idx}"
        )
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]

        # ПЕРЕВІРКА НА ЗАБОРОНУ (для звичайних користувачів)
        forbidden_ids = sess.get("forbidden", [])[self.idx]
        if user.id in forbidden_ids:
            return await inter.response.send_message(
                "⛔ Ви не можете претендувати на цей слот (заборонено).", ephemeral=True
            )

        for s in sessions.values():
            if s["channel_id"] == sess["channel_id"] and user in s["owners"]:
                return await inter.response.send_message(
                    "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                )

        key = (self.sid, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message(
                "ℹ️ Ви вже подали заявку.", ephemeral=True
            )
        lst.append(user)
        await inter.response.send_message("✅ Заявка прийнята.", ephemeral=True)

        global request_counter
        request_counter += 1
 
        embed = discord.Embed(
            title=f"📝 Заявка #{request_counter}",
            description=sess["title"],
            color=discord.Color.orange()
        )
        embed.add_field(name="Слот #", value=str(self.idx+1), inline=True)
        embed.add_field(
            name="Власник",
            value=(sess["owners"][self.idx].mention
                  if sess["owners"][self.idx] else "Вільний"),
            inline=True
        )
        embed.add_field(name="Кандидат", value=user.mention, inline=False)

        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            msg = await admin_ch.send(embed=embed)
            await msg.edit(view=ClaimDecisionView(self.sid, self.idx, user.id, msg.id))

class ClaimSlotView(View):
    def __init__(self, sid: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(sid, idx))

# ─── 8. Modal для рішення ───────────────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int,
        accept: bool
    ):
        title = "Причина призначення" if accept else "Причина відмови"
        super().__init__(title=title)
        self.sid = sid
        self.idx = idx
        self.claimant_id = claimant_id
        self.admin_msg_id = admin_msg_id
        self.accept = accept
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        key = (self.sid, self.idx)
        claimant = await bot.fetch_user(self.claimant_id)
        old_owner = sess["owners"][self.idx]
        reason = self.reason.value

        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop(key, None)
        else:
            lst = claims.get(key, [])
            if claimant in lst:
                lst.remove(claimant)

        # Оновлюємо головне повідомлення
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        # DM користувачам
        try:
            if self.accept:
                # ЗМІНА: Додано ID сесії, причину не відправляємо призначеному
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid})."
                )
                if old_owner and old_owner != claimant:
                    # ЗМІНА: Додано ID сесії, причину відправляємо знятому
                    await old_owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention} у «{sess['title']}» (ID: {self.sid}).\n"
                        f"Причина: {reason}"
                    )
            else:
                # ЗМІНА: Додано ID сесії
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid}) відхилена.\n"
                    f"Причина: {reason}"
                )
        except:
            pass

        # Видаляємо адмін-повідомлення
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            try:
                admin_msg = await admin_ch.fetch_message(self.admin_msg_id)
                await admin_msg.delete()
            except:
                pass

        await inter.response.send_message("✔️ Готово.", ephemeral=True)

class ClaimDecisionButton(Button):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int,
        accept: bool
    ):
        label = "✅ Призначити" if accept else "❌ Відхилити"
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        tag = "accept" if accept else "deny"
        super().__init__(
            label=label,
            style=style,
            custom_id=f"dec-{tag}-{sid}-{idx}-{claimant_id}-{admin_msg_id}"
        )
        self.sid = sid
        self.idx = idx
        self.claimant_id = claimant_id
        self.admin_msg_id = admin_msg_id
        self.accept = accept

    async def callback(self, inter: discord.Interaction):
        modal = DecisionModal(
            self.sid,
            self.idx,
            self.claimant_id,
            self.admin_msg_id,
            self.accept
        )
        await inter.response.send_modal(modal)

class ClaimDecisionView(View):
    def __init__(
        self,
        sid: int,
        idx: int,
        claimant_id: int,
        admin_msg_id: int
    ):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, claimant_id, admin_msg_id, False))

# ─── 9. Зняття через кнопки та Modal ───────────────────────────────────────────
class RemoveSlotModal(Modal):
    def __init__(self, sid: int, idx: int):
        super().__init__(title="Причина звільнення")
        self.sid, self.idx = sid, idx
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        reason = self.reason.value

        if not owner:
            return await inter.response.send_message(
                f"⚠️ Слот #{self.idx+1} вже вільний.", ephemeral=True
            )

        sess["owners"][self.idx] = None
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        try:
            # ЗМІНА: Додано ID сесії
            await owner.send(
                f"❗ Ви звільнені зі слоту #{self.idx+1} у «{sess['title']}» (ID: {self.sid}).\n"
                f"Причина: {reason}"
            )
        except:
            pass

        await inter.response.send_message(
            f"✅ Слот #{self.idx+1} звільнено.", ephemeral=True
        )

class RemoveSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(
            label=str(idx+1),
            style=discord.ButtonStyle.danger,
            custom_id=f"remove-{sid}-{idx}"
        )
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(RemoveSlotModal(self.sid, self.idx))

class RemoveSlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(RemoveSlotButton(sid, idx))

@bot.command(name="зняти", aliases=["release"])
async def зняти(ctx: commands.Context, session_msg_id: int):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(
        f"📋 Оберіть слот для звільнення в сесії {session_msg_id}:",
        view=RemoveSlotView(session_msg_id)
    )

# ─── 9.5. Команда !записати ─────────────────────────────────────────────────────
@bot.command(name="записати")
async def записати(ctx: commands.Context, session_msg_id: int, member: discord.Member):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send(f"❌ Сесія з ID {session_msg_id} не знайдена.")
    await ctx.send(
        f"📋 Оберіть слот для запису {member.mention} в сесії {session_msg_id}:",
        view=AssignSlotView(session_msg_id, member.id)
    )

class AssignSlotModal(Modal):
    def __init__(self, sid: int, idx: int, uid: int):
        super().__init__(title="Причина запису")
        self.sid, self.idx, self.uid = sid, idx, uid
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        user = await bot.fetch_user(self.uid)
        reason = self.reason.value

        if sess["owners"][self.idx] == user:
            return await inter.response.send_message(
                f"⚠️ {user.mention} вже записаний на слот #{self.idx+1}.", ephemeral=True
            )
        if sess["owners"][self.idx] is not None:
            return await inter.response.send_message(
                f"⚠️ Слот #{self.idx+1} вже зайнятий {sess['owners'][self.idx].mention}.", 
                ephemeral=True
            )

        sess["owners"][self.idx] = user
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(self.sid)
                await msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        try:
            # ЗМІНА: Додано ID сесії, причину не відправляємо
            await user.send(
                f"✅ Вас записано на слот #{self.idx+1} у «{sess['title']}» (ID: {self.sid})."
            )
        except:
            pass

        await inter.response.send_message(
            f"📌 {user.mention} записано на слот #{self.idx+1}.", ephemeral=True
        )

class AssignSlotButton(Button):
    def __init__(self, sid: int, idx: int, uid: int):
        super().__init__(
            label=str(idx+1),
            style=discord.ButtonStyle.success,
            custom_id=f"assign-{sid}-{idx}-{uid}"
        )
        self.sid, self.idx, self.uid = sid, idx, uid

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(AssignSlotModal(self.sid, self.idx, self.uid))

class AssignSlotView(View):
    def __init__(self, sid: int, uid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(AssignSlotButton(sid, idx, uid))

# ─── 10. PBO Upload Flow ─────────────────────────────────────────────────────────
# Тимчасове сховище для PBO-сесій (поки адмін обирає сторону/групи)
pbo_sessions: dict[int, dict] = {}  # message_id → { west: [...], east: [...] }

PBO_API_URL = "https://pbo.arma-plan-maker.com/slots"

SIDE_LABELS = {
    "west":  "🔵 BLUFOR / WEST",
    "east":  "🔴 OPFOR / EAST",
    "independent": "🟢 Independent",
    "civilian": "⚪ Civilian",
}

def side_label(key: str) -> str:
    return SIDE_LABELS.get(key.lower(), key.upper())


class PboSideSelect(Select):
    """Крок 1: вибір сторони."""
    def __init__(self, msg_id: int, sides: list[str]):
        self.msg_id = msg_id
        options = [SelectOption(label=side_label(s), value=s) for s in sides]
        super().__init__(
            placeholder="Оберіть сторону...",
            options=options,
            custom_id=f"pbo-side-{msg_id}"
        )

    async def callback(self, inter: discord.Interaction):
        side = self.values[0]
        data = pbo_sessions.get(self.msg_id)
        if not data:
            return await inter.response.send_message("❌ Сесія застаріла.", ephemeral=True)

        groups = data["slots"].get(side, [])
        if not groups:
            return await inter.response.send_message("❌ Немає груп для цієї сторони.", ephemeral=True)

        data["selected_side"] = side
        view = PboGroupSelectView(self.msg_id, groups)
        await inter.response.edit_message(
            content=f"**{side_label(side)}** — оберіть групи для публікації (можна декілька):",
            view=view
        )


class PboSideView(View):
    def __init__(self, msg_id: int, sides: list[str]):
        super().__init__(timeout=300)
        self.add_item(PboSideSelect(msg_id, sides))


class PboGroupSelect(Select):
    """Крок 2: мультивибір груп (callsigns)."""
    def __init__(self, msg_id: int, groups: list[dict], batch: int = 0):
        self.msg_id = msg_id
        self.batch = batch
        # Discord дозволяє max 25 опцій у Select
        chunk = groups[batch*25:(batch+1)*25]
        options = []
        for g in chunk:
            label = g["callsign"]
            # Перший юніт — підзаголовок
            desc_raw = g["units"][0]["name"] if g["units"] else ""
            # Беремо частину після "|" якщо є
            parts = desc_raw.split("|")
            desc = parts[1].strip() if len(parts) > 1 else desc_raw
            desc = desc[:100]
            options.append(SelectOption(label=label, description=desc, value=label))
        super().__init__(
            placeholder=f"Оберіть групи (до 25)...",
            options=options,
            min_values=1,
            max_values=len(options),
            custom_id=f"pbo-group-{msg_id}-{batch}"
        )

    async def callback(self, inter: discord.Interaction):
        data = pbo_sessions.get(self.msg_id)
        if not data:
            return await inter.response.send_message("❌ Сесія застаріла.", ephemeral=True)

        side = data["selected_side"]
        selected_callsigns = set(self.values)
        groups = data["slots"].get(side, [])
        chosen = [g for g in groups if g["callsign"] in selected_callsigns]

        if not chosen:
            return await inter.response.send_message("❌ Немає обраних груп.", ephemeral=True)

        await inter.response.edit_message(
            content=f"⏳ Публікую {len(chosen)} груп(и)...",
            view=None
        )

        channel = inter.channel
        NUMBER_RE = re.compile(r'^\d+\.\s*')

        for group in chosen:
            callsign = group["callsign"]
            units = group["units"]
            at_marker = f"@{callsign}"

            # ── Заголовок ──
            first_name = units[0]["name"] if units else callsign
            if at_marker in first_name:
                after_at = first_name.split(at_marker, 1)[1]
                parts = [p.strip() for p in after_at.split("|") if p.strip()]
                # Беремо перші N-1 частини (без локації в кінці)
                group_desc = " | ".join(parts[:-1]) if len(parts) > 1 else " | ".join(parts)
                title = f"{callsign}  ·  {group_desc}" if group_desc else callsign
            else:
                title = callsign

            # ── Рядки слотів — видаляємо нумерацію і @-мітку ──
            lines = []
            for u in units:
                name = u["name"]
                if at_marker in name:
                    name = name.split(at_marker, 1)[0]
                name = NUMBER_RE.sub("", name).strip()
                lines.append(name)

            owners = [None] * len(lines)
            forbidden_matrix = [[] for _ in lines]

            sess = {
                "title":      title,
                "lines":      lines,
                "owners":     owners,
                "channel_id": channel.id,
                "forbidden":  forbidden_matrix,
                "side":       side,
            }
            embed = build_embed(sess)
            sent = await channel.send(embed=embed)
            sessions[sent.id] = sess
            await sent.edit(view=SlotView(sent.id))

        # ── Видалення проміжних повідомлень ──
        msgs_to_delete = data.get("messages_to_delete", [])
        for mid in msgs_to_delete:
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except Exception:
                pass

        # Видаляємо статус-повідомлення ("Публікую...")
        try:
            status = await channel.fetch_message(self.msg_id)
            await status.delete()
        except Exception:
            pass

        pbo_sessions.pop(self.msg_id, None)


class PboGroupSelectView(View):
    def __init__(self, msg_id: int, groups: list[dict]):
        super().__init__(timeout=300)
        # Якщо груп більше 25 — розбиваємо на батчі (кілька Select)
        # Discord дозволяє max 5 Select у View
        total_batches = min(5, (len(groups) + 24) // 25)
        for b in range(total_batches):
            self.add_item(PboGroupSelect(msg_id, groups, b))


@bot.command(name="pbo")
async def _pbo(ctx: commands.Context):
    """Завантажити .pbo файл і обрати слоти для публікації."""
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Ця команда доступна лише в адміністративному каналі.")

    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть .pbo файл до повідомлення.")

    att = ctx.message.attachments[0]
    if not att.filename.lower().endswith(".pbo"):
        return await ctx.send("❌ Файл повинен мати розширення `.pbo`.")

    status_msg = await ctx.send("⏳ Завантажую та парсую PBO...")

    try:
        file_bytes = await att.read()
        async with aiohttp.ClientSession() as http:
            form = aiohttp.FormData()
            form.add_field("pbo", file_bytes, filename=att.filename, content_type="application/octet-stream")
            async with http.post(PBO_API_URL, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return await status_msg.edit(content=f"❌ API повернув {resp.status}:\n```{text[:500]}```")
                result = await resp.json()
    except Exception as e:
        return await status_msg.edit(content=f"❌ Помилка при запиті до API:\n```{e}```")

    slots_data = result.get("slots", {})
    available_sides = [k for k, v in slots_data.items() if v]

    if not available_sides:
        return await status_msg.edit(content="❌ У файлі не знайдено жодних слотів.")

    files_count = result.get("filesCount", "?")
    fname = result.get("fileName", att.filename)

    pbo_sessions[status_msg.id] = {
        "slots": slots_data,
        "selected_side": None,
        "messages_to_delete": [ctx.message.id],  # зберігаємо !pbo команду юзера
    }

    sides_text = " | ".join(side_label(s) for s in available_sides)
    await status_msg.edit(
        content=(
            f"✅ **{fname}** розпарсено ({files_count} файлів)\n"
            f"Знайдено сторони: {sides_text}\n\n"
            f"**Оберіть сторону:**"
        ),
        view=PboSideView(status_msg.id, available_sides)
    )


# ─── 10. Події on_ready та on_message ────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    for guild in bot.guilds:
        ch = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
                      and c.permissions_for(guild.me).send_messages,
            guild.text_channels
        )
        if ch:
            try:
                await ch.send(embed=embed)
            except:
                pass
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return

    if "запис слоти" in message.content.lower():
        processed_messages.add(message.id)
        header = None
        slots = []
        owners = []
        forbidden_matrix = [] # Список списків ID (per slot)
        
        # Регулярний вираз для пошуку "заборонити @люди" (case-insensitive)
        FORBIDDEN_CLEAN_RE = re.compile(r'\s*заборонити\s*(\s*(?:<@!?(?P<id>\d+)>|\s|,|[^,>])+\s*)$', re.I)

        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                raw_content = m.group(2)
                
                line_owner = None
                line_forbidden = []
                final_text = raw_content
                
                # 1. Парсинг заборони та ВИДАЛЕННЯ тексту
                match_forbidden = FORBIDDEN_CLEAN_RE.search(raw_content)
                
                if match_forbidden:
                    # 1.1. Витягуємо список заборонених ID з знайденої частини
                    forbidden_part = match_forbidden.group(1)
                    for id_match in MENTION_RE.finditer(forbidden_part):
                        line_forbidden.append(int(id_match.group('id')))
                        
                    # 1.2. Видаляємо частину з "заборонити" з тексту слота
                    final_text = raw_content[:match_forbidden.start()]
                
                # 2. Визначення власника (шукаємо згадку в оригінальному *raw_content*)
                
                # Визначаємо, чи є в слоті згадка користувача, який НЕ є в списку заборонених
                potential_owner_mentions = [
                    u for u in message.mentions 
                    if u.id not in line_forbidden
                    and (f"<@{u.id}>" in raw_content or f"<@!{u.id}>" in raw_content)
                ]
                
                # Якщо є явна згадка користувача, який не в списку заборон, робимо його власником.
                if potential_owner_mentions:
                    line_owner = potential_owner_mentions[0]

                # 3. Видалення ЗГАДКИ власника (якщо його знайдено)
                if line_owner:
                    # Видаляємо згадку власника з *вже очищеного* від "заборонити" тексту
                    final_text = re.sub(fr'<@!?{line_owner.id}>', '', final_text)

                # 4. Фінальна зачистка від зайвих пробілів/ком
                final_text = final_text.strip()
                final_text = re.sub(r'\s{2,}', ' ', final_text) 
                final_text = re.sub(r'[\s,.:;]+$', '', final_text)

                slots.append(final_text)
                owners.append(line_owner) 
                forbidden_matrix.append(line_forbidden)

            elif header is None:
                header = txt

        # Обрізаємо до 25 (ліміт Embed field/rows)
        slots = slots[:25]
        owners = owners[:len(slots)]
        forbidden_matrix = forbidden_matrix[:len(slots)]

        sess = {
            "title":      header or DEFAULT_TITLE,
            "lines":      slots,
            "owners":     owners,
            "channel_id": message.channel.id,
            "forbidden":  forbidden_matrix  # зберігаємо список заборонених
        }
        embed = build_embed(sess)
        sent  = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))

    await bot.process_commands(message)

# ─── 11. Сервісні команди ───────────────────────────────────────────────────────
@bot.command(name="оновити", aliases=["update"])
async def _оновити(ctx: commands.Context):
    if not DEPLOY_HOOK_URL:
        return await ctx.send("❌ DEPLOY_HOOK_URL не встановено")
    async with aiohttp.ClientSession() as sess:
        await sess.post(DEPLOY_HOOK_URL)
    await ctx.send("🔄 Деплой тригерено!")

@bot.command(name="статус", aliases=["status"])
async def _статус(ctx: commands.Context):
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    await ctx.send(
        f"🧠 Commit: `{commit}`\n"
        f"📊 Sessions: {len(sessions)}\n"
        f"📋 Claims: {sum(len(v) for v in claims.values())}"
    )

@bot.command(name="gitpush")
async def _gitpush(ctx: commands.Context):
    emb = discord.Embed(title="🛠 Git Push інструкція", color=discord.Color.orange())
    emb.add_field(name="1. cd до папки", value="`cd C:\\Users\\stas\\botslot`", inline=False)
    emb.add_field(name="2. git add",       value="`git add .`",                         inline=False)
    emb.add_field(name="3. git commit",    value='`git commit -m "Оновлення слота"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 12. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)
