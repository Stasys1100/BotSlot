import os
import re
import asyncio
import subprocess
import aiohttp
import datetime
from dotenv import load_dotenv
from keep_alive import keep_alive

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput, Select
from discord import SelectOption
from vtg_api import (
    VtgApiClient,
    normalize_callsign,
    clean_unit_role_name,
    format_forum_title,
    extract_group_list,
    KYIV_TZ
)

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
raw_admin_id = os.getenv("ADMIN_CHANNEL_ID")
ADMIN_CHANNEL_ID = int(raw_admin_id) if raw_admin_id and raw_admin_id.isdigit() else None
VTG_CHANNEL_ID         = int(os.getenv("VTG_CHANNEL_ID", "1160843618433630228"))
SLOTS_FORUM_CHANNEL_ID = int(os.getenv("SLOTS_FORUM_CHANNEL_ID", "1334559273073643694"))
TARGET_SQUAD_NAME      = os.getenv("TARGET_SQUAD_NAME", "28")
VTG_API_KEY            = os.getenv("VTG_API_KEY", "")

async def get_or_fetch_channel(channel_id: int):
    """Безпечне отримання каналу з кэшу або через API."""
    if not channel_id:
        return None
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return ch

processed_messages: set[int] = set()
# sessions: message_id → { title, lines, owners, channel_id, forbidden }
sessions: dict[int, dict] = {}            
claims: dict[tuple[int,int], list] = {}   # (message_id, idx) → [User, ...]
request_counter = 0                       # лічильник заявок
vtg_wizard_sessions: dict[int, dict] = {} # message_id → wizard data

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

# ─── 5. Палітра «Теплий мінімалізм» та генератор Embed ──────────────────────────
WARM_ACCENT = discord.Color.from_rgb(207, 148, 86)   # Теплий бурштин / пісок (#CF9456)
WARM_BLUE   = discord.Color.from_rgb(86, 126, 160)   # М'який теплий сланець (#567EA0)
WARM_RED    = discord.Color.from_rgb(194, 98, 77)    # Теракота / глина (#C2624D)
WARM_GREEN  = discord.Color.from_rgb(112, 142, 106)  # Тепла шавлія / мох (#708E6A)
WARM_GRAY   = discord.Color.from_rgb(150, 142, 133)  # Теплий тауп / камінь (#968E85)

SIDE_COLORS = {
    "west":        WARM_BLUE,
    "east":        WARM_RED,
    "independent": WARM_GREEN,
    "civilian":    WARM_GRAY,
    "blue":        WARM_BLUE,
    "red":         WARM_RED,
}

def parse_group_metadata(group: dict) -> tuple[str, str]:
    fallback_callsign = (group.get("callsign") or "").strip()
    units = group.get("units") or []
    if not units:
        return fallback_callsign, ""

    first_unit = units[0] if isinstance(units[0], dict) else {}
    first_name = (first_unit.get("name") or "")
    at_idx = first_name.find("@")
    if at_idx == -1:
        return fallback_callsign, ""

    after_at = first_name[at_idx + 1:].strip()
    
    pipe_idx = after_at.find("|")
    if pipe_idx != -1:
        raw_callsign = after_at[:pipe_idx].strip()
        remainder = after_at[pipe_idx + 1:].strip()
    else:
        raw_callsign = after_at.strip()
        remainder = ""

    parts = [p.strip() for p in remainder.split("|") if p.strip()]
    group_desc = " | ".join(parts)

    callsign = fallback_callsign
    if raw_callsign:
        m = re.search(r'(\d+)\s*-\s*(\d+)', raw_callsign)
        if m:
            callsign = f"Alpha {m.group(1)}-{m.group(2)}"
        else:
            callsign = raw_callsign

    return callsign.strip(), group_desc.strip()

def build_embed(sess: dict) -> discord.Embed:
    side = sess.get("side", "west")
    color = SIDE_COLORS.get(side, WARM_BLUE)
    embed = discord.Embed(title=sess["title"], color=color)

    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        num = f"`{i+1}.`"
        slot_text = text.strip()
        if owner:
            owner_id = owner if isinstance(owner, int) else getattr(owner, "id", None)
            lines.append(f"{num} {slot_text}\n> <@{owner_id}>")
        else:
            lines.append(f"{num} {slot_text}")
        lines.append("")   # порожній рядок між слотами

    # Прибираємо останній зайвий порожній рядок
    if lines and lines[-1] == "":
        lines.pop()

    embed.description = "\n".join(lines)

    total = len(sess["lines"])
    taken = sum(1 for o in sess["owners"] if o is not None)
    free  = total - taken
    embed.set_footer(text=f"Вільно: {free} · Зайнято: {taken} · Всього: {total}")
    return embed


# ─── 5.5 Admin Slot Control ─────────────────────────────────────────────────────
class AdminUserSelect(discord.ui.UserSelect):
    def __init__(self, sid: int, idx: int):
        super().__init__(placeholder="Виберіть бійця...", min_values=1, max_values=1)
        self.sid = sid
        self.idx = idx

    async def callback(self, inter: discord.Interaction):
        selected_user = self.values[0]
        sess = sessions[self.sid]
        
        # Remove from other slots in the same channel (game)
        for s in sessions.values():
            if s.get("channel_id") == sess.get("channel_id"):
                for i, o in enumerate(s.get("owners", [])):
                    if o == selected_user.id or getattr(o, "id", None) == selected_user.id:
                        s["owners"][i] = None
        
        # Assign to this slot
        sess["owners"][self.idx] = selected_user.id
        
        # Update main message
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main_msg = await ch.fetch_message(self.sid)
                await main_msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass
                
        # Send DM
        try:
            await selected_user.send(f"🎖️ Вас призначено на слот #{self.idx+1} у «{sess['title']}» адміністратором.")
        except:
            pass
            
        await inter.response.defer()
        try:
            if hasattr(self.view, 'original_inter'):
                await self.view.original_inter.delete_original_response()
            else:
                await inter.message.delete()
        except Exception as e:
            print("Delete error:", e)

class AdminControlTakeButton(discord.ui.Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(label="Зайняти собі", style=discord.ButtonStyle.success)
        self.sid = sid
        self.idx = idx
    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        
        # Release old slots in same channel
        for s in sessions.values():
            if s.get("channel_id") == sess.get("channel_id"):
                for i, o in enumerate(s.get("owners", [])):
                    if o == user.id or getattr(o, "id", None) == user.id:
                        s["owners"][i] = None
                        
        sess["owners"][self.idx] = user.id
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main_msg = await ch.fetch_message(self.sid)
                await main_msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass
        await inter.response.defer()
        try:
            if hasattr(self.view, 'original_inter'):
                await self.view.original_inter.delete_original_response()
            else:
                await inter.message.delete()
        except Exception as e:
            print("Delete error:", e)

class AdminControlReleaseButton(discord.ui.Button):
    def __init__(self, sid: int, idx: int, disabled: bool):
        super().__init__(label="Звільнити бійця", style=discord.ButtonStyle.danger, disabled=disabled)
        self.sid = sid
        self.idx = idx
    async def callback(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        old_owner = sess["owners"][self.idx]
        sess["owners"][self.idx] = None
        
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main_msg = await ch.fetch_message(self.sid)
                await main_msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass
                
        # Try sending DM
        if old_owner:
            owner_id = old_owner if isinstance(old_owner, int) else getattr(old_owner, 'id', None)
            if owner_id:
                try:
                    u = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
                    if u:
                        await u.send(f"❗ Адміністратор звільнив вас зі слоту #{self.idx+1} у «{sess['title']}».")
                except:
                    pass
                        
        await inter.response.defer()
        try:
            if hasattr(self.view, 'original_inter'):
                await self.view.original_inter.delete_original_response()
            else:
                await inter.message.delete()
        except Exception as e:
            print("Delete error:", e)

class AdminControlCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Закрити", style=discord.ButtonStyle.secondary)
    async def callback(self, inter: discord.Interaction):
        await inter.response.defer()
        try:
            if hasattr(self.view, 'original_inter'):
                await self.view.original_inter.delete_original_response()
            else:
                await inter.message.delete()
        except Exception as e:
            print("Delete error:", e)

class AdminSlotControlView(discord.ui.View):
    def __init__(self, sid: int, idx: int, original_inter: discord.Interaction):
        super().__init__(timeout=300)
        self.original_inter = original_inter
        sess = sessions.get(sid, {})
        owner = sess.get("owners", [None])[idx]
        
        self.add_item(AdminUserSelect(sid, idx))
        self.add_item(AdminControlTakeButton(sid, idx))
        self.add_item(AdminControlReleaseButton(sid, idx, disabled=(owner is None)))
        self.add_item(AdminControlCloseButton())


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
        if self.sid not in sessions:
            await reconstruct_session(inter.message)
        sess = sessions.get(self.sid)
        if not sess:
            return await inter.response.send_message("❌ Помилка: сесія не знайдена. Спробуйте оновити тему.", ephemeral=True)
            
        owner = sess["owners"][self.idx]
        ch_id = sess["channel_id"]

        is_admin = False
        if hasattr(inter, "permissions") and getattr(inter.permissions, "manage_messages", False):
            is_admin = True
        elif getattr(user, "guild_permissions", None) and user.guild_permissions.administrator:
            is_admin = True
            
        if is_admin:
            slot_name = sess["lines"][self.idx]
            owner_display = f"<@{owner}>" if owner else "Вільний"
            
            embed = discord.Embed(
                title=f"Керування слотом #{self.idx+1}",
                description=f"**Тема:** {sess['title']}\n**Слот:** {slot_name}\n**Статус:** {owner_display}\n\nОберіть дію нижче:",
                color=discord.Color.gold()
            )
            return await inter.response.send_message(embed=embed, view=AdminSlotControlView(self.sid, self.idx, inter), ephemeral=True)

        # ПЕРЕВІРКА НА ЗАБОРОНУ (для звичайних користувачів)
        forbidden_ids = sess.get("forbidden", [])[self.idx]
        if user.id in forbidden_ids:
            return await inter.response.send_message(
                "⛔ Цей слот заборонено для вас.", ephemeral=True
            )

        # 6.1) Вільний слот → зайняти
        if owner is None:
            for s in sessions.values():
                if s["channel_id"] == ch_id and user.id in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user.id
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        # 6.2) Свій слот → звільнити
        if owner == user.id or owner == user:
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
            for idx in range(min(25, len(sessions[sid]["lines"]))):
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
            if s["channel_id"] == sess["channel_id"] and user.id in s["owners"]:
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
            value=(f"<@{sess['owners'][self.idx]}>" if sess["owners"][self.idx] else "Вільний"),
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
            sess["owners"][self.idx] = claimant.id
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
                if old_owner and old_owner != claimant.id:
                    old_owner_user = bot.get_user(old_owner) or await bot.fetch_user(old_owner)
                    # ЗМІНА: Додано ID сесії, причину відправляємо знятому
                    await old_owner_user.send(
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
            owner_user = bot.get_user(owner) or await bot.fetch_user(owner)
            if owner_user:
                await owner_user.send(
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
            for idx in range(min(25, len(sessions[sid]["lines"]))):
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
            for idx in range(min(25, len(sessions[sid]["lines"]))):
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
    def __init__(self, msg_id: int, groups: list[dict], batch: int = 0, total_batches: int = 1):
        self.msg_id = msg_id
        self.batch = batch
        # Discord дозволяє max 25 опцій у Select
        chunk = groups[batch*25:(batch+1)*25]
        options = []
        for i, g in enumerate(chunk):
            global_idx = batch * 25 + i
            callsign, desc = parse_group_metadata(g)
            callsign = (callsign or "").strip()
            if not callsign:
                callsign = f"Група {global_idx + 1}"
            label = callsign[:100]

            units = g.get("units") or []
            units_count = len(units)
            if desc:
                desc_text = f"{desc} ({units_count} сл.)"
            else:
                desc_text = f"{units_count} сл."
            desc_val = desc_text[:100] if desc_text else "Група"

            # Використовуємо global_idx як унікальний value для кожної опції
            options.append(SelectOption(label=label, description=desc_val, value=str(global_idx)))

        placeholder = (
            f"Групи {batch*25 + 1}–{min((batch+1)*25, len(groups))} (частина {batch+1}/{total_batches})..."
            if total_batches > 1
            else "Оберіть групи (до 25)..."
        )
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=max(1, len(options)),
            custom_id=f"pbo-group-{msg_id}-{batch}"
        )

    async def callback(self, inter: discord.Interaction):
        data = pbo_sessions.get(self.msg_id)
        if not data:
            return await inter.response.send_message("❌ Сесія застаріла.", ephemeral=True)

        side = data["selected_side"]
        groups = data["slots"].get(side, [])

        selected_indices = set()
        for v in self.values:
            try:
                selected_indices.add(int(v))
            except ValueError:
                pass

        chosen = [groups[i] for i in sorted(selected_indices) if 0 <= i < len(groups)]

        if not chosen:
            return await inter.response.send_message("❌ Немає обраних груп.", ephemeral=True)

        await inter.response.edit_message(
            content=f"⏳ Публікую {len(chosen)} груп(и)...",
            view=None
        )

        channel = inter.channel
        NUMBER_RE = re.compile(r'^\d+[\.:]\s*')

        for group in chosen:
            units = group.get("units") or []
            meta_callsign, group_desc = parse_group_metadata(group)
            meta_callsign = (meta_callsign or "").strip()
            title = f"{meta_callsign} | {group_desc}" if group_desc else meta_callsign
            if not title.strip():
                title = f"Група {side_label(side)}"

            # ── Рядки слотів — видаляємо нумерацію і всю @-мітку з хвостом ──
            lines = []
            for u in units:
                name = (u.get("name") if isinstance(u, dict) else "") or ""
                # Видаляємо від "@" до кінця рядка (включно з "| Група | Транспорт | Локація")
                at_pos = name.find("@")
                if at_pos != -1:
                    name = name[:at_pos]
                # Видаляємо початкову нумерацію "1. ", "2. " тощо
                name = NUMBER_RE.sub("", name).strip().rstrip("|").strip()
                lines.append(name or "Слот")

            if not lines:
                continue

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
            self.add_item(PboGroupSelect(msg_id, groups, b, total_batches=total_batches))


@bot.command(name="pbo")
async def _pbo(ctx: commands.Context):
    """Завантажити .pbo файл і обрати слоти для публікації."""
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


# ─── 10.5. Інтеграція з VTG API та Forum слотування ───────────────────────────────

class VtgGameSelect(Select):
    def __init__(self, games: list[dict], caller_id: int):
        self.games = games
        self.caller_id = caller_id
        options = []
        for i, g in enumerate(games):
            pos = g.get("position", i) + 1
            m_name = (g.get("mission") or {}).get("name", "Місія")
            raw_date = g.get("date", "")
            date_str = ""
            if raw_date:
                try:
                    dt = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(KYIV_TZ)
                    date_str = dt.strftime("%d.%m.%Y")
                except Exception:
                    date_str = raw_date[:10]
            label = f"Гра {pos}: {m_name}"[:100]
            ver_num = (g.get("missionVersion") or {}).get("version", "")
            desc = f"{date_str} · Версія {ver_num}" if ver_num else date_str
            options.append(SelectOption(label=label, description=desc[:100], value=str(i)))

        super().__init__(
            placeholder="Оберіть гру уікенду...",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, inter: discord.Interaction):
        if inter.user.id != self.caller_id:
            return await inter.response.send_message("Це меню викликано іншим адміністратором.", ephemeral=True)

        game_idx = int(self.values[0])
        game = self.games[game_idx]
        await inter.response.defer()

        api_key = (os.getenv("VTG_API_KEY") or "").strip().strip("'\"")
        client = VtgApiClient(api_key)

        mission = game.get("mission") or {}
        version = game.get("missionVersion") or {}
        mission_id = mission.get("id")
        version_id = version.get("id")

        if not mission_id or not version_id:
            return await inter.followup.send("У обраної гри відсутній ID місії або версії.", ephemeral=True)

        try:
            side_type, role_name, info = await client.detect_squad_side(game, TARGET_SQUAD_NAME)
            slots_data = await client.get_mission_slots(mission_id, version_id)
        except Exception as e:
            return await inter.followup.send(f"Помилка звернення до VTG API: `{e}`", ephemeral=True)

        # Отримуємо плаский список груп для обраної сторони
        groups = extract_group_list(slots_data, side_type, role_name)
        if not groups:
            return await inter.followup.send(f"Не знайдено відділень для сторони `{side_type}` у відповіді API.", ephemeral=True)

        vtg_wizard_sessions[inter.message.id] = {
            "game": game,
            "side_type": side_type,
            "role_name": role_name,
            "info": info,
            "slots_data": slots_data,
            "groups": groups,
            "selected_indices": set(),
            "caller_id": self.caller_id
        }

        view = VtgSquadSelectView(inter.message.id)
        embed = build_vtg_squad_select_embed(vtg_wizard_sessions[inter.message.id])
        await inter.message.edit(content=None, embed=embed, view=view)


class VtgGameSelectView(View):
    def __init__(self, games: list[dict], caller_id: int):
        super().__init__(timeout=300)
        self.add_item(VtgGameSelect(games, caller_id))


class VtgSquadSelect(Select):
    def __init__(self, wizard_msg_id: int, groups: list[dict], batch: int = 0, total_batches: int = 1):
        self.wizard_msg_id = wizard_msg_id
        self.batch = batch
        chunk = groups[batch*25:(batch+1)*25]
        options = []
        for i, g in enumerate(chunk):
            global_idx = batch * 25 + i
            raw_callsign = (g.get("callsign") or f"Група {global_idx+1}").strip()
            norm_callsign = normalize_callsign(raw_callsign)
            meta_cs, desc = parse_group_metadata(g)
            label = f"{norm_callsign} · {raw_callsign}"[:100] if norm_callsign != raw_callsign else raw_callsign[:100]

            units = g.get("units") or []
            count = g.get("count") or len(units)
            first_weapon = ""
            if units:
                first_name = units[0].get("name", "") if isinstance(units[0], dict) else str(units[0])
                parts = first_name.split("|")
                if len(parts) > 1:
                    first_weapon = parts[-1].strip()

            desc_text = f"{count} сл."
            if first_weapon:
                desc_text += f" · {first_weapon}"

            options.append(SelectOption(label=label, description=desc_text[:100], value=str(global_idx)))

        placeholder = (
            f"Відділення {batch*25 + 1}–{min((batch+1)*25, len(groups))} ({batch+1}/{total_batches})..."
            if total_batches > 1
            else "Оберіть відділення для участі..."
        )
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=0,
            max_values=max(1, len(options)),
            custom_id=f"vtg-squad-{wizard_msg_id}-{batch}"
        )

    async def callback(self, inter: discord.Interaction):
        data = vtg_wizard_sessions.get(self.wizard_msg_id)
        if not data:
            return await inter.response.send_message("Сесія майстра застаріла.", ephemeral=True)
        if inter.user.id != data["caller_id"]:
            return await inter.response.send_message("Це меню викликано іншим адміністратором.", ephemeral=True)

        batch_indices = set(range(self.batch * 25, min((self.batch + 1) * 25, len(data["groups"]))))
        data["selected_indices"] = (data["selected_indices"] - batch_indices) | {int(v) for v in self.values if v.isdigit()}
        await inter.response.defer()


class VtgPublishButton(Button):
    def __init__(self, wizard_msg_id: int):
        super().__init__(
            label="Опублікувати слоти",
            style=discord.ButtonStyle.primary,
            custom_id=f"vtg-publish-{wizard_msg_id}"
        )
        self.wizard_msg_id = wizard_msg_id

    async def callback(self, inter: discord.Interaction):
        data = vtg_wizard_sessions.get(self.wizard_msg_id)
        if not data:
            return await inter.response.send_message("Сесія майстра застаріла.", ephemeral=True)
        if inter.user.id != data["caller_id"]:
            return await inter.response.send_message("Це меню викликано іншим адміністратором.", ephemeral=True)

        selected_indices = sorted(data.get("selected_indices", set()))
        if not selected_indices:
            return await inter.response.send_message(
                "Оберіть потрібні відділення у списку вище.",
                ephemeral=True
            )

        await inter.response.defer()
        loading_embed = discord.Embed(
            title="⏳ Створення теми на форумі...",
            description="Зачекайте, формуємо слоти...",
            color=discord.Color.blue()
        )
        await inter.message.edit(content=None, embed=loading_embed, view=None)

        game = data["game"]
        mission = game.get("mission") or {}
        mission_name = (mission.get("name") or "Місія").strip()
        side_type = str(data["side_type"]).upper()
        role_name = data["role_name"]
        game_pos = game.get("position", 0) + 1

        raw_date = game.get("date", "")
        date_str = ""
        if raw_date:
            try:
                dt = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(KYIV_TZ)
                date_str = dt.strftime("%d.%m.%Y")
            except Exception:
                date_str = raw_date[:10]

        groups = data["groups"]
        chosen_groups = [groups[i] for i in selected_indices if 0 <= i < len(groups)]
        callsigns = [normalize_callsign(g.get("callsign", "")) for g in chosen_groups]

        title = format_forum_title(date_str, mission_name, callsigns, game_pos, role_name)

        forum_ch = await get_or_fetch_channel(SLOTS_FORUM_CHANNEL_ID)
        if not forum_ch:
            return await inter.followup.send(
                f"Не вдалося знайти форум-канал слотування з ID `{SLOTS_FORUM_CHANNEL_ID}`. Перевірте права бота."
            )

        side_color = SIDE_COLORS.get(side_type.lower(), WARM_ACCENT)
        starter_embed = discord.Embed(
            title=mission_name,
            description=(
                f"Дата: **{date_str}** · Гра **№{game_pos}**\n"
                f"Сторона: **{role_name}** ({side_type})\n"
                f"Відділення: **{', '.join(callsigns)}**\n\n"
                f"*Займіть свій слот за допомогою кнопок під списками відділень.*"
            ),
            color=side_color
        )

        try:
            if isinstance(forum_ch, discord.ForumChannel):
                thread_with_msg = await forum_ch.create_thread(
                    name=title,
                    embed=starter_embed
                )
                thread = getattr(thread_with_msg, "thread", thread_with_msg)
            elif isinstance(forum_ch, discord.TextChannel):
                thread = await forum_ch.create_thread(
                    name=title,
                    type=discord.ChannelType.public_thread
                )
                await thread.send(embed=starter_embed)
            else:
                return await inter.followup.send(f"Канал `{SLOTS_FORUM_CHANNEL_ID}` не підтримує публікації тем.")
        except Exception as e:
            return await inter.followup.send(f"Помилка при створенні теми у форумі: `{e}`")

        # Публікація слотів для кожного обраного відділення
        published_count = 0
        for group in chosen_groups:
            units = group.get("units") or []
            lines = []
            for u in units:
                raw_u_name = u.get("name") if isinstance(u, dict) else str(u)
                lines.append(clean_unit_role_name(raw_u_name))

            if not lines:
                cnt = group.get("count", 1)
                lines = [f"Слот {j+1}" for j in range(cnt)]

            callsign = (group.get("callsign") or "Відділення").strip()
            meta_cs, desc = parse_group_metadata(group)
            group_title = f"{callsign} | {desc}" if desc else callsign

            owners = [None] * len(lines)
            forbidden_matrix = [[] for _ in lines]

            sess = {
                "title": group_title,
                "lines": lines,
                "owners": owners,
                "channel_id": thread.id,
                "forbidden": forbidden_matrix,
                "side": side_type.lower()
            }
            embed = build_embed(sess)
            sent = await thread.send(embed=embed)
            sessions[sent.id] = sess
            await sent.edit(view=SlotView(sent.id))
            published_count += 1
            await asyncio.sleep(0.5)

        thread_url = getattr(thread, "jump_url", f"https://discord.com/channels/{inter.guild_id}/{thread.id}")
        success_embed = discord.Embed(
            title="✅ Тему успішно опубліковано",
            description=f"**{title}**\n[🔗 Перейти до слотів]({thread_url})",
            color=discord.Color.green()
        )
        success_embed.set_footer(text=f"Опубліковано відділень: {published_count}")
        await inter.message.edit(content=None, embed=success_embed, view=None)
        vtg_wizard_sessions.pop(self.wizard_msg_id, None)


def build_vtg_squad_select_embed(data: dict) -> discord.Embed:
    game = data["game"]
    mission = game.get("mission") or {}
    mission_name = (mission.get("name") or "Місія").strip()
    side_type = str(data.get("side_type", "")).upper()
    role_name = data.get("role_name", "")
    game_pos = game.get("position", 0) + 1

    raw_date = game.get("date", "")
    date_str = ""
    if raw_date:
        try:
            dt = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(KYIV_TZ)
            date_str = dt.strftime("%d.%m.%Y")
        except Exception:
            date_str = raw_date[:10]

    info = (data.get("info") or "").strip()
    info_line = f"\n{info}" if info else ""

    side_color = SIDE_COLORS.get(side_type.lower(), WARM_ACCENT)

    embed = discord.Embed(
        title=f"Налаштування слотів — {mission_name}",
        description=(
            f"Дата: **{date_str}** · Гра **№{game_pos}**\n"
            f"Сторона: **{role_name}** ({side_type}){info_line}\n\n"
            f"*Оберіть потрібні відділення у списку нижче та натисніть «Опублікувати слоти».*"
        ),
        color=side_color
    )
    return embed


class VtgSquadSelectView(View):
    def __init__(self, wizard_msg_id: int):
        super().__init__(timeout=300)
        data = vtg_wizard_sessions.get(wizard_msg_id)
        if not data:
            return
        groups = data["groups"]
        total_batches = min(4, (len(groups) + 24) // 25)
        for b in range(total_batches):
            self.add_item(VtgSquadSelect(wizard_msg_id, groups, b, total_batches=total_batches))
        self.add_item(VtgPublishButton(wizard_msg_id))


@bot.command(name="слоти", aliases=["slots", "vtg_slots", "week"])
async def _слоти(ctx: commands.Context):
    """Вибрати гру уікенду та створити тему слотування на форумі."""
    if ADMIN_CHANNEL_ID and ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("Ця команда доступна лише в адміністративному каналі.")

    api_key = (os.getenv("VTG_API_KEY") or "").strip().strip("'\"")
    if not api_key:
        return await ctx.send(
            "**VTG_API_KEY не налаштовано.**\n"
            "Додайте `VTG_API_KEY` у змінні середовища та перезапустіть бота."
        )

    status_msg = await ctx.send("Завантаження розкладу ігор...")

    client = VtgApiClient(api_key)
    try:
        weekend, games = await client.get_current_week_games()
    except Exception as e:
        return await status_msg.edit(content=f"Помилка звернення до VTG API: `{e}`")

    if not games:
        return await status_msg.edit(content="Не знайдено опублікованих ігор для поточного уікенду.")

    wk_name = weekend.get("name", "Уікенд") if weekend else "Поточний уікенд"
    embed = discord.Embed(
        title=f"{wk_name} · Вибір місії",
        description=(
            f"Знайдено {len(games)} гри на уікенді.\n"
            f"Оберіть потрібну зі списку нижче:"
        ),
        color=WARM_ACCENT
    )

    view = VtgGameSelectView(games, ctx.author.id)
    await status_msg.edit(content=None, embed=embed, view=view)


# ─── 11. Події on_ready та on_message ────────────────────────────────────────────
BOT_LOG_CHANNEL_ID = 1395065909185478769


async def reconstruct_session(msg: discord.Message):
    if msg.id in sessions:
        return
    embed = msg.embeds[0]
    title = embed.title
    desc = embed.description or ""
    
    slots = []
    owners = []
    
    blocks = desc.split("\n\n")
    for block in blocks:
        lines = block.split("\n")
        slot_text_match = re.search(r'`\d+\.`\s*(.*)', lines[0])
        slot_text = slot_text_match.group(1) if slot_text_match else lines[0]
        slots.append(slot_text)
        
        if len(lines) > 1 and lines[1].startswith("> "):
            owner_match = re.search(r'<@!?(\d+)>', lines[1])
            if owner_match:
                owners.append(int(owner_match.group(1)))
            else:
                owners.append(None)
        else:
            owners.append(None)
            
    sessions[msg.id] = {
        "title": title,
        "lines": slots,
        "owners": owners,
        "channel_id": msg.channel.id,
        "forbidden": [[] for _ in slots]
    }

async def setup_hook():
    pass

bot.setup_hook = setup_hook


async def recover_sessions():
    print("Recovering sessions from recent messages...")
    channels_to_check = [VTG_CHANNEL_ID, SLOTS_FORUM_CHANNEL_ID]
    for channel_id in channels_to_check:
        if not channel_id: continue
        ch = bot.get_channel(channel_id)
        if not ch: continue
        
        targets = []
        if isinstance(ch, discord.ForumChannel):
            for t in ch.threads:
                if t not in targets: targets.append(t)
            if hasattr(ch.guild, 'threads'):
                for t in ch.guild.threads:
                    if t.parent_id == ch.id and t not in targets:
                        targets.append(t)
        else:
            targets.append(ch)
            
        for t in targets:
            try:
                async for msg in t.history(limit=25):
                    if msg.author == bot.user and msg.embeds:
                        embed = msg.embeds[0]
                        if embed.footer and embed.footer.text and "Вільно:" in embed.footer.text:
                            await reconstruct_session(msg)
            except Exception as e:
                pass

    print(f"Re-registering views for {len(sessions)} sessions...")
    for sid, sess in sessions.items():
        bot.add_view(SlotView(sid))
        if 'RemoveSlotView' in globals():
            bot.add_view(RemoveSlotView(sid))
        for idx in range(min(25, len(sess.get("lines", [])))):
            bot.add_view(ClaimSlotView(sid, idx))

@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user}")
    commit = subprocess.getoutput("git rev-parse --short HEAD")
    embed = discord.Embed(
        title="🔄 Бот перезапущено",
        description=f"📦 Commit: `{commit}`",
        color=discord.Color.green()
    )
    log_ch = bot.get_channel(BOT_LOG_CHANNEL_ID)
    if log_ch:
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass
    if not vtg_reminder.is_running():
        vtg_reminder.start()
    bot.loop.create_task(recover_sessions())

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
                owners.append(line_owner.id if line_owner else None) 
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
