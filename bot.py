import os
import re
import subprocess
import aiohttp
import datetime
import io
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
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))

# ─── 2. Інтенти та ініціалізація бота ───────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── 3. Конфігурація ────────────────────────────────────────────────────────────
KYIV_TZ          = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID   = 1160843618433630228

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}            
claims: dict[tuple[int,int], list] = {}   
request_counter = 0                       

TRIGGER_RE    = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE    = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Prikaati 'Karhu' | Jalkaväen haara"

# ─── 4. SQM PARSER (ВИПРАВЛЕНО) ────────────────────────────────────────────────
class SqmParser:
    def __init__(self):
        # Слова-тригери для визначення початку нової групи
        self.sl_patterns = [
            r"командир", r"squad leader", r"komandir", r"nodalas komandieris", 
            r"sl", r"sql", r"officer", r"офіцер", r"lidem", r"vadītājs", r"sergeant",
            r"leader", r"ст\.", r"старший"
        ]
        self.index_pattern = re.compile(r"\b(\d+-\d+)\b")
        self.side_pattern = re.compile(r'side="?(\w+)"?;', re.IGNORECASE)

    def _clean_role_name(self, text):
        """
        Очищає назву ролі.
        Вхід: '1. Командир відділення@Альфа 1-4 (AK-74)'
        Вихід: 'Командир відділення'
        """
        # 1. Прибираємо зброю в дужках тимчасово
        text = re.sub(r'\(.*?\)', '', text)
        # 2. Прибираємо нумерацію на початку (1. 2.)
        text = re.sub(r'^\s*\d+[\.\)]\s*', '', text)
        # 3. Розділяємо по @. Беремо ЛІВУ частину (це роль)
        if "@" in text:
            text = text.split("@")[0]
        return text.strip()

    def _normalize_slot(self, text):
        """Формує фінальний рядок слота."""
        # Шукаємо зброю в оригінальному тексті
        weapons = re.findall(r'\((.*?)\)', text)
        best_weapon = max(weapons, key=len) if weapons else ""

        # Отримуємо чисту назву ролі
        role_clean = self._clean_role_name(text)
        
        final_parts = [role_clean]
        if best_weapon:
            final_parts.append(f"({best_weapon})")
        
        return " ".join(final_parts)

    def _extract_group_index(self, text):
        match = self.index_pattern.search(text)
        return match.group(1) if match else None

    def process_file(self, file_content_str):
        lines = file_content_str.splitlines()
        raw_units = [] 
        current_side = "UNKNOWN"
        
        # Етап 1: Збір юнітів із файлу
        for line in lines:
            line = line.strip()
            # Пошук зміни сторони
            side_match = self.side_pattern.search(line)
            if side_match:
                current_side = side_match.group(1).upper()
            
            # Пошук тексту
            if line.startswith('text=') or line.startswith('description='):
                match = re.search(r'"(.*)"', line)
                if match:
                    raw_text = match.group(1)
                    if raw_text and not raw_text.startswith("__"):
                        raw_units.append({'side': current_side, 'text': raw_text})

        # Етап 2: Групування
        groups_map = {} 
        current_squad_slots = []
        current_squad_side = None
        
        # Маркер кінця файлу
        raw_units.append({'side': 'END', 'text': 'END_MARKER'})

        for unit in raw_units:
            text = unit['text']
            side = unit['side']
            
            # Чи це командир (початок нової групи)?
            is_sl = any(re.search(pat, text, re.IGNORECASE) for pat in self.sl_patterns)
            
            # Умова закриття попередньої групи
            should_close = (is_sl and current_squad_slots) or \
                           (current_squad_side is not None and side != current_squad_side)

            if should_close:
                if current_squad_slots:
                    first_raw = current_squad_slots[0]
                    # Перевіряємо ще раз, чи перший слот - командир
                    if any(re.search(pat, first_raw, re.IGNORECASE) for pat in self.sl_patterns):
                        
                        g_idx = self._extract_group_index(first_raw)
                        group_name = ""

                        # --- Логіка витягування назви (ПРАВА частина від @) ---
                        if "@" in first_raw:
                            parts = first_raw.split("@", 1)
                            # Права частина - назва групи
                            group_name = parts[1].strip()
                            # Прибираємо зброю з назви групи, якщо вона там є
                            group_name = re.sub(r'\(.*?\)', '', group_name).strip()
                        else:
                            # Фолбек: якщо @ немає
                            name_buffer = first_raw
                            for pat in self.sl_patterns:
                                name_buffer = re.sub(pat, "", name_buffer, flags=re.IGNORECASE)
                            group_name = re.sub(r'\(.*?\)', '', name_buffer).replace('|', '').strip().strip("-").strip()

                        if not group_name:
                            group_name = f"Squad {g_idx}" if g_idx else "Infantry"
                        
                        full_title = f"[{current_squad_side}] {group_name}"

                        # --- Формування слотів ---
                        formatted_slots = []
                        for i, raw_s in enumerate(current_squad_slots):
                            formatted_slots.append(f"{i+1}. {self._normalize_slot(raw_s)}")
                        
                        # --- КЛЮЧ УНІКАЛЬНОСТІ (ВИПРАВЛЕНО) ---
                        # Тепер ключ включає назву групи. Це запобігає злиттю WEST Alpha і EAST Alpha.
                        unique_key = (current_squad_side, group_name)

                        new_group = {
                            'title': full_title,
                            'pure_name': group_name,
                            'slots': formatted_slots,
                            'side': current_squad_side,
                            'group_index': g_idx
                        }

                        # Якщо така група вже є (наприклад, дублікат в файлі),
                        # залишаємо ту, де слотів більше (або просто перезаписуємо)
                        groups_map[unique_key] = new_group

                current_squad_slots = []

            if text != 'END_MARKER':
                current_squad_slots.append(text)
                if len(current_squad_slots) == 1:
                    current_squad_side = side

        # Сортування: спочатку за стороною, потім за назвою
        return sorted(groups_map.values(), key=lambda x: (x['side'], x['title']))

sqm_parser = SqmParser()
# ─── 5. Логіка відображення ─────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def vtg_reminder():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4, 6) and now.hour == 19 and now.minute == 30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch:
            try: await ch.send("||@everyone||\n**Сбор VTG**")
            except: pass

def build_embed(sess: dict) -> discord.Embed:
    embed = discord.Embed(title=sess["title"], color=discord.Color.blue())
    lines = []
    for i, (text, owner) in enumerate(zip(sess["lines"], sess["owners"])):
        prefix = f"{i+1}. "
        if owner:
            lines.append(f"{prefix}{text} – Зайнято {owner.mention}")
        else:
            lines.append(f"{prefix}{text}")
    embed.description = "\n".join(lines)
    return embed

class SlotButton(Button):
    def __init__(self, sid: int, idx: int):
        owner = sessions[sid]["owners"][idx]
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

        if owner is None:
            # Перевірка на мульти-слот
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message("⚠️ Ви вже маєте слот.", ephemeral=True)
            sess["owners"][self.idx] = user
        elif owner == user:
            sess["owners"][self.idx] = None
        else:
            return await inter.response.send_message(f"⚠️ Зайнято {owner.mention}.", view=ClaimSlotView(self.sid, self.idx), ephemeral=True)
            
        await inter.response.edit_message(embed=build_embed(sess), view=SlotView(self.sid))

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(SlotButton(sid, idx))

class ClaimSlotButton(Button):
    def __init__(self, sid: int, idx: int):
        super().__init__(label="❗ Претендувати", style=discord.ButtonStyle.primary, custom_id=f"claim-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, inter: discord.Interaction):
        user = inter.user
        sess = sessions[self.sid]
        for s in sessions.values():
            if s["channel_id"] == sess["channel_id"] and user in s["owners"]:
                return await inter.response.send_message("⚠️ Ви вже маєте слот.", ephemeral=True)
        
        key = (self.sid, self.idx)
        lst = claims.setdefault(key, [])
        if user in lst:
            return await inter.response.send_message("ℹ️ Заявка вже є.", ephemeral=True)
        lst.append(user)
        await inter.response.send_message("✅ Заявка надіслана.", ephemeral=True)
        
        global request_counter
        request_counter += 1
        embed = discord.Embed(title=f"📝 Заявка #{request_counter}", description=sess["title"], color=discord.Color.orange())
        embed.add_field(name="Слот", value=str(self.idx+1))
        embed.add_field(name="Власник", value=sess["owners"][self.idx].mention if sess["owners"][self.idx] else "Вільний")
        embed.add_field(name="Кандидат", value=user.mention, inline=False)
        
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            msg = await admin_ch.send(embed=embed)
            await msg.edit(view=ClaimDecisionView(self.sid, self.idx, user.id, msg.id))

class ClaimSlotView(View):
    def __init__(self, sid: int, idx: int):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(sid, idx))
        # ─── 6. Адмін-інструментарій ────────────────────────────────────────────────────
class DecisionModal(Modal):
    def __init__(self, sid, idx, uid, msg_id, accept):
        super().__init__(title="Рішення")
        self.sid, self.idx, self.uid, self.msg_id, self.accept = sid, idx, uid, msg_id, accept
        self.reason = TextInput(label="Причина", style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, inter: discord.Interaction):
        sess = sessions[self.sid]
        claimant = await bot.fetch_user(self.uid)
        
        if self.accept:
            sess["owners"][self.idx] = claimant
            claims.pop((self.sid, self.idx), None)
            try: await claimant.send(f"✅ Призначено. {self.reason.value}")
            except: pass
        else:
            try: await claimant.send(f"❌ Відмовлено. {self.reason.value}")
            except: pass
            
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try: await (await ch.fetch_message(self.sid)).edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass
            
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            try: await (await admin_ch.fetch_message(self.msg_id)).delete()
            except: pass
        await inter.response.send_message("✔️ OK.", ephemeral=True)

class ClaimDecisionView(View):
    def __init__(self, sid, idx, uid, msg_id):
        super().__init__(timeout=None)
        self.add_item(ClaimDecisionButton(sid, idx, uid, msg_id, True))
        self.add_item(ClaimDecisionButton(sid, idx, uid, msg_id, False))

class ClaimDecisionButton(Button):
    def __init__(self, sid, idx, uid, msg_id, accept):
        style = discord.ButtonStyle.success if accept else discord.ButtonStyle.danger
        label = "Так" if accept else "Ні"
        super().__init__(style=style, label=label)
        self.sid, self.idx, self.uid, self.msg_id, self.accept = sid, idx, uid, msg_id, accept
    
    async def callback(self, inter):
        await inter.response.send_modal(DecisionModal(self.sid, self.idx, self.uid, self.msg_id, self.accept))

class RemoveSlotModal(Modal):
    def __init__(self, sid, idx):
        super().__init__(title="Звільнення")
        self.sid, self.idx = sid, idx
        self.reason = TextInput(label="Причина")
        self.add_item(self.reason)

    async def on_submit(self, inter):
        sess = sessions[self.sid]
        owner = sess["owners"][self.idx]
        sess["owners"][self.idx] = None
        
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try: await (await ch.fetch_message(self.sid)).edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass
        
        if owner:
            try: await owner.send(f"❗ Звільнено. {self.reason.value}")
            except: pass
        await inter.response.send_message("✅ Готово.", ephemeral=True)

class RemoveSlotButton(Button):
    def __init__(self, sid, idx):
        super().__init__(label=str(idx+1), style=discord.ButtonStyle.danger, custom_id=f"rm-{sid}-{idx}")
        self.sid, self.idx = sid, idx
    async def callback(self, inter):
        await inter.response.send_modal(RemoveSlotModal(self.sid, self.idx))

class RemoveSlotView(View):
    def __init__(self, sid):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(RemoveSlotButton(sid, idx))

class AssignSlotModal(Modal):
    def __init__(self, sid, idx, uid):
        super().__init__(title="Запис")
        self.sid, self.idx, self.uid = sid, idx, uid
        self.reason = TextInput(label="Причина")
        self.add_item(self.reason)

    async def on_submit(self, inter):
        sess = sessions[self.sid]
        user = await bot.fetch_user(self.uid)
        sess["owners"][self.idx] = user
        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try: await (await ch.fetch_message(self.sid)).edit(embed=build_embed(sess), view=SlotView(self.sid))
            except: pass
        try: await user.send(f"✅ Вас записано. {self.reason.value}")
        except: pass
        await inter.response.send_message("✅ Готово.", ephemeral=True)

class AssignSlotButton(Button):
    def __init__(self, sid, idx, uid):
        super().__init__(label=str(idx+1), style=discord.ButtonStyle.success)
        self.sid, self.idx, self.uid = sid, idx, uid
    async def callback(self, inter):
        await inter.response.send_modal(AssignSlotModal(self.sid, self.idx, self.uid))

class AssignSlotView(View):
    def __init__(self, sid, uid):
        super().__init__(timeout=None)
        if sid in sessions:
            for idx in range(len(sessions[sid]["lines"])):
                self.add_item(AssignSlotButton(sid, idx, uid))
                # ─── 7. Команди та Події ────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not vtg_reminder.is_running():
        vtg_reminder.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in processed_messages:
        return
    # Стара система (ручний текст)
    if "запис слоти" in message.content.lower():
        processed_messages.add(message.id)
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower(): continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = next((u for u in message.mentions if f"<@{u.id}>" in txt), None)
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None: header = txt
        sess = {"title": header or DEFAULT_TITLE, "lines": slots[:25], "owners": owners[:len(slots)], "channel_id": message.channel.id}
        embed = build_embed(sess)
        sent = await message.channel.send(embed=embed)
        sessions[sent.id] = sess
        await sent.edit(view=SlotView(sent.id))
    await bot.process_commands(message)

@bot.command(name='import_sqm')
async def import_sqm(ctx, filter_idx: str = None):
    """
    Імпорт слотів.
    Використання: !import_sqm (всі) АБО !import_sqm 1-4
    """
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .sqm")

    status = await ctx.send("⏳ Обробка...")
    try:
        content = (await ctx.message.attachments[0].read()).decode('utf-8', errors='ignore')
        groups = sqm_parser.process_file(content)

        # Фільтрація
        if filter_idx:
            filtered = []
            low_f = filter_idx.lower()
            for g in groups:
                match_idx = (g['group_index'] == filter_idx)
                match_title = (low_f in g['title'].lower())
                if match_idx or match_title:
                    filtered.append(g)
            
            if not filtered:
                return await status.edit(content=f"❌ Нічого не знайдено для: `{filter_idx}`")
            groups = filtered

        if not groups:
            return await status.edit(content="⚠️ Відділень не знайдено.")

        await status.edit(content=f"✅ Знайдено: {len(groups)}")

        msg_buf = ""
        for group in groups:
            # Формат: заголовок + слоти в блоці коду
            block = f"**{group['title']}**\n```\n" + "\n".join(group['slots']) + "\n```\n"
            if len(msg_buf) + len(block) > 1900:
                await ctx.send(msg_buf)
                msg_buf = ""
            msg_buf += block
        
        if msg_buf:
            await ctx.send(msg_buf)
        await ctx.send("🏁 Імпорт завершено.")

    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="зняти")
async def admin_release(ctx, msg_id: int):
    if ctx.channel.id != ADMIN_CHANNEL_ID: return
    if msg_id not in sessions: return await ctx.send("❌ Не знайдено.")
    await ctx.send(f"Звільнення ({msg_id}):", view=RemoveSlotView(msg_id))

@bot.command(name="записати")
async def admin_assign(ctx, msg_id: int, member: discord.Member):
    if ctx.channel.id != ADMIN_CHANNEL_ID: return
    if msg_id not in sessions: return await ctx.send("❌ Не знайдено.")
    await ctx.send(f"Запис {member.display_name}:", view=AssignSlotView(msg_id, member.id))

@bot.command(name="оновити")
async def update_bot(ctx):
    if DEPLOY_HOOK_URL:
        async with aiohttp.ClientSession() as s: await s.post(DEPLOY_HOOK_URL)
        await ctx.send("🔄 Deploy triggered.")

@bot.command(name="статус")
async def status_bot(ctx):
    await ctx.send(f"Sessions: {len(sessions)}")

bot.run(TOKEN)
