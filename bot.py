# bot.py
import os
import re
import subprocess
import aiohttp
import datetime
import io
import zipfile
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv
from keep_alive import keep_alive

# Optional PBO library (if installed on host). If not installed — pbo = None
try:
    import pbo
except Exception:
    pbo = None

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
KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 1160843618433630228)
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 1395065909185478769)

processed_messages: set[int] = set()
sessions: dict[int, dict] = {}            # message_id → { title, lines, owners, channel_id }
claims: dict[tuple[int,int], list] = {}   # (message_id, idx) → [User, ...]
request_counter = 0                       # лічильник заявок

TRIGGER_RE = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENTION_RE = re.compile(r'<@!?(?P<id>\d+)>')
DEFAULT_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

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

# ─── 5. Embed генератори ───────────────────────────────────────────────────────
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

def build_group_embed(title: str, slots: List[str]) -> discord.Embed:
    embed = discord.Embed(title=title or "Відділення", color=discord.Color.blurple())
    if slots:
        lines = [f"{i+1}. {s}" for i, s in enumerate(slots)]
        embed.description = "\n".join(lines)
    else:
        embed.description = "— слотів не знайдено —"
    return embed

# ─── 6. Покращений RAP декодер ─────────────────────────────────────────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n[ \t]+', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def _clean_token(tok: str) -> str:
    tok = tok.strip()
    # видалити залишки байтових артефактів
    tok = tok.replace('\x00', '').replace('\ufffd', '')
    # замінити weird quotes
    tok = tok.replace('“', '"').replace('”', '"').replace("’", "'").replace("‘", "'")
    # видалити зайві символи, що часто з'являються у RAP
    tok = re.sub(r'[\x01-\x1f\x7f-\x9f]', '', tok)
    tok = tok.strip()
    return tok

def rap_to_text_aggressive(data: bytes) -> str:
    """
    Агресивний RAP → текст декодер:
    - декодує як latin-1, utf-8 fallback
    - фільтрує нечитаємі символи
    - збирає великі фрагменти навколо ключових слів
    - нормалізує синтаксис для парсера
    """
    # 1) Спроба utf-8, потім latin-1
    try:
        raw = data.decode('utf-8')
        used = 'utf-8'
    except Exception:
        raw = data.decode('latin-1', errors='ignore')
        used = 'latin-1'

    # 2) Замінити непотрібні control-символи на пробіли
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+', ' ', raw)

    # 3) Фільтрація: залишаємо читабельні блоки (ASCII, кирилиця, базова пунктуація)
    def keep(ch):
        o = ord(ch)
        return (32 <= o <= 126) or (0x0400 <= o <= 0x04FF) or ch in '\n\r\t{}=;"\'.,:-_()[]/\\'
    filtered = ''.join(ch if keep(ch) else ' ' for ch in raw)

    # 4) Знайти індекси ключових слів і збирати вікна
    keywords = ['class Group', 'class Unit', 'groupName', 'unitName', 'description', 'name', 'title']
    indices = []
    for kw in keywords:
        start = 0
        while True:
            idx = filtered.lower().find(kw.lower(), start)
            if idx == -1:
                break
            indices.append(idx)
            start = idx + len(kw)

    if indices:
        window = 4000
        frags = []
        for idx in sorted(set(indices)):
            s = max(0, idx - 300)
            e = min(len(filtered), idx + window)
            frags.append(filtered[s:e])
        candidate = '\n'.join(frags)
    else:
        # якщо ключових слів немає — беремо весь фільтрований текст, але скорочуємо
        candidate = filtered[:200000]

    # 5) Нормалізація синтаксису: перенос рядків після ; і };
    candidate = candidate.replace('};', '};\n')
    candidate = re.sub(r';\s*', ';\n', candidate)
    candidate = re.sub(r'\{\s*', '{\n', candidate)
    candidate = re.sub(r'\s*\}\s*', '\n}\n', candidate)

    # 6) Очищення токенів
    lines = [_clean_token(l) for l in candidate.splitlines()]
    candidate = '\n'.join([l for l in lines if l.strip()])

    # 7) Додаткові спрощення: замінити подвійні пробіли, нормалізувати лапки
    candidate = _normalize_whitespace(candidate)
    candidate = candidate.replace("= '", '="').replace("= '", '="').replace("= \"", '="')
    candidate = candidate.replace("'", '"')

    # 8) Додати заголовок, щоб парсер мав контекст
    return f"// rap_decoded (encoding={used})\n{candidate}"

# ─── 7. Гнучкий парсер mission.sqm ─────────────────────────────────────────────
def parse_mission_sqm_flexible(text: str) -> List[Tuple[str, List[str]]]:
    """
    Гнучкий парсер:
    - шукає повні блоки class Group { ... };
    - якщо немає — шукає groupName/name/title і збирає unit-блоки поруч;
    - фолбек — збирає всі unitName/description у тексті і групує по найближчому маркеру групи.
    Повертає список (group_name, [slot1, slot2, ...]).
    """
    groups: List[Tuple[str, List[str]]] = []

    # Нормалізувати лапки і пробіли
    txt = text.replace('\r\n', '\n')

    # 1) Повні блоки class Group { ... };
    group_blocks = re.findall(r'(class\s+Group\b.*?\{.*?\}[\s;]*)', txt, flags=re.IGNORECASE | re.DOTALL)
    if group_blocks:
        for blk in group_blocks:
            # знайти назву групи
            mname = re.search(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', blk, flags=re.IGNORECASE)
            gname = mname.group("v").strip() if mname else "Відділення"
            # знайти unit-блоки
            units = re.findall(r'class\s+Unit\b.*?\{(.*?)\}', blk, flags=re.IGNORECASE | re.DOTALL)
            slots = []
            for u in units:
                mslot = re.search(r'(?:description|unitName|text)\s*=\s*"?(?P<v>[^";\n]+)"?', u, flags=re.IGNORECASE)
                if mslot:
                    slots.append(mslot.group("v").strip())
                else:
                    # інколи роль вказана як name = "..."
                    mslot2 = re.search(r'(?:name)\s*=\s*"?(?P<v>[^";\n]+)"?', u, flags=re.IGNORECASE)
                    slots.append(mslot2.group("v").strip() if mslot2 else "Слот")
            groups.append((gname, slots))
        return groups

    # 2) Якщо немає блоків — шукати groupName/name/title і брати unitName/description поруч
    for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', txt, flags=re.IGNORECASE):
        gname = m.group("v").strip()
        start = m.end()
        frag = txt[start:start + 30000]  # вікно 30k символів
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', frag, flags=re.IGNORECASE)
        if units:
            groups.append((gname, [u.strip() for u in units]))

    if groups:
        return groups

    # 3) Фолбек: знайти всі unitName/description у тексті і згрупувати
    unit_matches = [(m.start(), m.group("v").strip()) for m in re.finditer(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', txt, flags=re.IGNORECASE)]
    group_markers = [(m.start(), m.group(0)) for m in re.finditer(r'(?:class\s+Group\b|groupName|name|title)', txt, flags=re.IGNORECASE)]
    if unit_matches:
        if not group_markers:
            return [("Відділення", [u for _, u in unit_matches])]
        # групуємо по найближчому попередньому маркеру
        grouped: Dict[int, List[str]] = {}
        marker_positions = [pos for pos, _ in group_markers]
        for pos, uname in unit_matches:
            # знайти останній маркер перед pos
            prev_positions = [p for p in marker_positions if p <= pos]
            key = max(prev_positions) if prev_positions else -1
            grouped.setdefault(key, []).append(uname)
        for key, slots in grouped.items():
            if key == -1:
                gname = "Відділення"
            else:
                # знайти ім'я групи в околі ключа
                snippet = txt[key:key+200]
                mname = re.search(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', snippet, flags=re.IGNORECASE)
                gname = mname.group("v").strip() if mname else "Відділення"
            groups.append((gname, slots))
        return groups

    # 4) Нічого не знайдено
    return []

# ─── 8. Витяг з PBO та читання вкладень ───────────────────────────────────────
def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    """
    Повертає raw bytes mission.sqm з PBO або None.
    Використовує пакет pbo якщо встановлено, інакше zip-фолбек.
    """
    if pbo:
        try:
            archive = pbo.PBO(io.BytesIO(pbo_bytes))
            for name in archive.list():
                if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                    return archive.read(name)
        except Exception:
            pass

    try:
        z = zipfile.ZipFile(io.BytesIO(pbo_bytes))
        for name in z.namelist():
            if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                return z.read(name)
    except Exception:
        pass

    return None

async def read_attachment_sqm_text(attachment: discord.Attachment) -> Tuple[str, str]:
    """
    Повертає (text, method):
    - Якщо .pbo → бере mission.sqm (raw bytes) і декодує;
    - Якщо .sqm → визначає RAP або текст і повертає текст для парсингу.
    """
    data = await attachment.read()
    filename = attachment.filename.lower()

    # .pbo: витягнути mission.sqm (raw bytes)
    if filename.endswith(".pbo"):
        sqm_raw = extract_mission_from_pbo_bytes(data)
        if not sqm_raw:
            # Якщо не знайшли явно — пробуємо декодувати як RAP фрагменти з самого PBO
            text = rap_to_text_aggressive(data)
            return text, "pbo-rap-fragments"
        # Якщо знайдено raw mission.sqm — визначаємо формат
        if sqm_raw[:3] == b'raP':
            text = rap_to_text_aggressive(sqm_raw)
            return text, "pbo-rap"
        # Інакше — пробуємо стандартні кодування
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return sqm_raw.decode(enc), f"pbo-{enc}"
            except Exception:
                continue
        return sqm_raw.decode("latin-1", errors="ignore"), "pbo-latin1-fallback"

    # .sqm: визначити RAP чи текст
    if filename.endswith(".sqm"):
        if data[:3] == b'raP':
            text = rap_to_text_aggressive(data)
            return text, "rap"
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                return data.decode(enc), enc
            except Exception:
                continue
        return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

    # інші файли: фолбек
    if data[:3] == b'raP':
        return rap_to_text_aggressive(data), "rap-raw"
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("latin-1", errors="ignore"), "latin-1-fallback"

# ─── 9. SlotButton та SlotView ─────────────────────────────────────────────────
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
            for s in sessions.values():
                if s["channel_id"] == ch_id and user in s["owners"]:
                    return await inter.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            sess["owners"][self.idx] = user
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        if owner == user:
            sess["owners"][self.idx] = None
            return await inter.response.edit_message(
                embed=build_embed(sess), view=SlotView(self.sid)
            )

        return await inter.response.send_message(
            f"⚠️ Цей слот зайнято {owner.mention}.",
            view=ClaimSlotView(self.sid, self.idx),
            ephemeral=True
        )

class SlotView(View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(SlotButton(sid, idx))

# ─── 10. Інші UI класи та команди (Claim, Assign, Remove) — без змін ──────────
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

        ch = bot.get_channel(sess["channel_id"])
        if ch:
            try:
                main = await ch.fetch_message(self.sid)
                await main.edit(embed=build_embed(sess), view=SlotView(self.sid))
            except:
                pass

        try:
            if self.accept:
                await claimant.send(
                    f"✅ Вас призначено на слот #{self.idx+1} у «{sess['title']}».\n"
                    f"Причина: {reason}"
                )
                if old_owner and old_owner != claimant:
                    await old_owner.send(
                        f"⚠️ Ваш слот #{self.idx+1} передано {claimant.mention}.\n"
                        f"Причина: {reason}"
                    )
            else:
                await claimant.send(
                    f"❌ Ваша заявка на слот #{self.idx+1} у «{sess['title']}» відхилена.\n"
                    f"Причина: {reason}"
                )
        except:
            pass

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

# ─── 11. Команда імпорту mission.sqm / .pbo з діагностикою ────────────────────
@bot.command(name="імпорт_sqm")
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]
    try:
        text, method = await read_attachment_sqm_text(attachment)
    except Exception as e:
        return await ctx.send(f"❌ Не вдалося прочитати вкладення: {e}")

    # 1) Основний парсер
    groups = parse_mission_sqm_flexible(text)

    # 2) Якщо нічого — додаткові фолбеки: простий пошук по ключовим словам
    diagnostics = {
        "method": method,
        "found_groups": 0,
        "found_unit_tokens": 0,
        "sample_fragments": []
    }

    if not groups:
        # знайти всі unitName/description/text
        unit_tokens = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', text, flags=re.IGNORECASE)
        diagnostics["found_unit_tokens"] = len(unit_tokens)
        # знайти маркери груп
        group_markers = re.findall(r'(?:class\s+Group\b|groupName|name|title)', text, flags=re.IGNORECASE)
        # простий фолбек: якщо є groupName і unit_tokens — агрегувати
        if group_markers and unit_tokens:
            # знайти перші 10 groupName occurrences and take following unit tokens windows
            groups = []
            for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"?(?P<v>[^";\n]+)"?', text, flags=re.IGNORECASE):
                gname = m.group("v").strip()
                frag = text[m.end(): m.end() + 20000]
                units = re.findall(r'(?:unitName|description|text)\s*=\s*"?(?P<v>[^";\n]+)"?', frag, flags=re.IGNORECASE)
                if units:
                    groups.append((gname, [u.strip() for u in units]))
            diagnostics["found_groups"] = len(groups)
        else:
            # агресивний фрагментний пошук: зібрати фрагменти навколо ключових слів для діагностики
            for kw in ["class Group", "class Unit", "groupName", "unitName", "description", "name"]:
                idx = text.lower().find(kw.lower())
                if idx != -1:
                    s = max(0, idx - 200)
                    e = min(len(text), idx + 800)
                    diagnostics["sample_fragments"].append(text[s:e])
            # якщо unit_tokens є — повернути їх як одна група
            if unit_tokens:
                groups = [("Відділення", [u.strip() for u in unit_tokens])]
                diagnostics["found_groups"] = 1

    else:
        diagnostics["found_groups"] = len(groups)
        # підрахувати unit tokens
        diagnostics["found_unit_tokens"] = sum(len(slots) for _, slots in groups)

    # 3) Якщо все ще нічого — повернути діагностику
    if not groups:
        preview = "\n".join(text.splitlines()[:60])[:1900]
        emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.orange())
        emb.add_field(name="Метод обробки", value=diagnostics["method"], inline=False)
        emb.add_field(name="Знайдено unit tokens", value=str(diagnostics["found_unit_tokens"]), inline=True)
        emb.add_field(name="Знайдено груп (фолбек)", value=str(diagnostics["found_groups"]), inline=True)
        if diagnostics["sample_fragments"]:
            emb.add_field(name="Приклади фрагментів", value=diagnostics["sample_fragments"][0][:1000], inline=False)
        emb.add_field(name="Прев'ю початку файлу", value=f"```{preview}```", inline=False)
        await ctx.send(embed=emb)
        return

    # 4) Відправити знайдені групи як embed-и
    target_ch = bot.get_channel(ADMIN_CHANNEL_ID)
    if not target_ch:
        return await ctx.send("❌ Адмін-канал не знайдено за ID.")

    sent_count = 0
    for group_name, slot_list in groups:
        # очистити слоти від артефактів
        cleaned_slots = []
        for s in slot_list:
            s2 = _clean_token(s)
            # якщо рядок містить шлях до файлу або клас юніта — спробувати спростити
            s2 = re.sub(r'\\+', '/', s2)
            s2 = re.sub(r'\.sqf\b', '', s2, flags=re.IGNORECASE)
            s2 = s2.strip()
            if not s2:
                s2 = "Слот"
            cleaned_slots.append(s2)
        embed = build_group_embed(group_name, cleaned_slots[:25])
        try:
            await target_ch.send(embed=embed)
            sent_count += 1
        except Exception:
            pass

    # 5) Відправити коротку діагностику в канал виклику
    await ctx.send(f"✅ Імпорт завершено. Опубліковано відділень: {sent_count}. Метод обробки: `{method}`. Знайдено слотів: {sum(len(slots) for _, slots in groups)}.")

# ─── 12. on_ready та on_message ────────────────────────────────────────────────
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
        header, slots, owners = None, [], []
        for line in message.content.splitlines():
            txt = line.strip()
            if not txt or "запис слоти" in txt.lower() or "everyone" in txt.lower():
                continue
            m = TRIGGER_RE.match(txt)
            if m:
                owner = next(
                    (u for u in message.mentions
                     if f"<@{u.id}>" in txt or f"<@!{u.id}>" in txt),
                    None
                )
                clean = MENTION_RE.sub("", m.group(2)).strip()
                slots.append(clean)
                owners.append(owner)
            elif header is None:
                header = txt

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

# ─── 13. Сервісні команди ───────────────────────────────────────────────────────
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
    emb.add_field(name="3. git commit",    value='`git commit -m \"Оновлення слота\"`', inline=False)
    emb.add_field(name="4. git push",      value="`git push origin main`",             inline=False)
    emb.set_footer(text="Після push → !оновити")
    await ctx.send(embed=emb)

# ─── 14. Запуск бота ─────────────────────────────────────────────────────────────
bot.run(TOKEN)
