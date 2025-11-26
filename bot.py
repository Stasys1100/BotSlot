# bot.py
# Discord бот: завантажує .pbo/.sqm на зовнішній сервіс (EXTRACTOR_API_URL),
# отримує розпакований/декодований текст або JSON з відділеннями і повертає тільки відділення та слоти.
# Мінімальні змінні в .env: DISCORD_TOKEN, ADMIN_CHANNEL_ID, EXTRACTOR_API_URL (і опціонально EXTRACTOR_API_KEY).

import os
import re
import io
import json
import html
import zipfile
import datetime
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Dict, Any

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ─── Конфігурація ───────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID") or 0)
VTG_CHANNEL_ID = int(os.getenv("VTG_CHANNEL_ID") or 0)
EXTRACTOR_API_URL = os.getenv("EXTRACTOR_API_URL")  # наприклад https://fileproinfo.com/api/upload
EXTRACTOR_API_KEY = os.getenv("EXTRACTOR_API_KEY")  # опціонально

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# ─── Утиліти для очищення і парсингу ────────────────────────────────────────
def _normalize_whitespace(s: str) -> str:
    s = re.sub(r'\r\n|\r', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def clean_slot_value(raw: str) -> str:
    if raw is None:
        return "Слот"
    s = html.unescape(raw)
    s = re.sub(r'<\s*t\b[^>]*>(.*?)<\s*/\s*t\s*>', lambda m: m.group(1), s, flags=re.IGNORECASE|re.DOTALL)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' "\'')
    s = re.sub(r'\.sqf\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\.pbo\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bhttps?://\S+\b', '', s)
    s = s.strip()
    return s or "Слот"

def parse_mission_sqm_flexible(text: str) -> List[Tuple[str, List[str]]]:
    groups: List[Tuple[str, List[str]]] = []
    txt = text.replace('\r\n', '\n')
    # 1) Try to extract class Group blocks
    group_blocks = re.findall(r'(class\s+Group\b.*?\{.*?\}[\s;]*)', txt, flags=re.IGNORECASE | re.DOTALL)
    if group_blocks:
        for blk in group_blocks:
            mname = re.search(r'(?:name|groupName|title)\s*=\s*"(.*?)"\s*;', blk, flags=re.IGNORECASE | re.DOTALL)
            if not mname:
                mname = re.search(r'(?:name|groupName|title)\s*=\s*[\'"](.*?)[\'"]', blk, flags=re.IGNORECASE | re.DOTALL)
            gname = clean_slot_value(mname.group(1)) if mname else "Відділення"
            units = re.findall(r'class\s+Unit\b.*?\{(.*?)\}', blk, flags=re.IGNORECASE | re.DOTALL)
            slots = []
            for u in units:
                mslot = re.search(r'(?:description|unitName|text)\s*=\s*"(.*?)"\s*;', u, flags=re.IGNORECASE | re.DOTALL)
                if not mslot:
                    mslot = re.search(r'(?:description|unitName|text)\s*=\s*[\'"](.*?)[\'"]', u, flags=re.IGNORECASE | re.DOTALL)
                if mslot:
                    slots.append(clean_slot_value(mslot.group(1)))
                    continue
                arr = re.findall(r'(?:description|unitName|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', u, flags=re.IGNORECASE | re.DOTALL)
                if arr:
                    items = re.findall(r'[\'"](.+?)[\'"]', arr[0], flags=re.DOTALL)
                    for it in items:
                        slots.append(clean_slot_value(it))
                    continue
                mslot2 = re.search(r'(?:name)\s*=\s*"(.*?)"\s*;', u, flags=re.IGNORECASE | re.DOTALL)
                if not mslot2:
                    mslot2 = re.search(r'(?:name)\s*=\s*[\'"](.*?)[\'"]', u, flags=re.IGNORECASE | re.DOTALL)
                if mslot2:
                    slots.append(clean_slot_value(mslot2.group(1)))
                    continue
                q = re.search(r'"([^"]{2,200})"', u, flags=re.DOTALL)
                slots.append(clean_slot_value(q.group(1)) if q else "Слот")
            groups.append((gname, slots))
        return groups

    # 2) Fallback: find name/title and following unitName/description/text
    for m in re.finditer(r'(?:name|groupName|title)\s*=\s*"(.*?)"\s*;', txt, flags=re.IGNORECASE | re.DOTALL):
        gname = clean_slot_value(m.group(1))
        start = m.end()
        frag = txt[start:start + 30000]
        units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"\s*;', frag, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            units = re.findall(r'(?:unitName|description|text)\s*=\s*[\'"](.*?)[\'"]', frag, flags=re.IGNORECASE | re.DOTALL)
        if not units:
            arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', frag, flags=re.IGNORECASE | re.DOTALL)
            for a in arrs:
                units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
        if units:
            groups.append((gname, [clean_slot_value(u) for u in units]))
    if groups:
        return groups

    # 3) Last resort: collect all unitName/description/text tokens
    unit_matches = [clean_slot_value(m.group(1)) for m in re.finditer(r'(?:unitName|description|text)\s*=\s*"(.*?)"', txt, flags=re.IGNORECASE | re.DOTALL)]
    if unit_matches:
        return [("Відділення", unit_matches)]
    return []

# ─── PBO helper ─────────────────────────────────────────────────────────────
def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(pbo_bytes))
        for name in z.namelist():
            if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                return z.read(name)
    except Exception:
        pass
    return None

# ─── Інтеграція з зовнішнім сервісом ────────────────────────────────────────
async def upload_to_service(attachment: discord.Attachment, timeout: int = 60) -> Dict[str, Any]:
    """
    Завантажує файл на EXTRACTOR_API_URL і повертає розпарений результат.
    Очікує JSON з полями 'departments' або 'decoded_text', або HTML/plain text.
    """
    if not EXTRACTOR_API_URL:
        return {"status": "no-config", "error": "EXTRACTOR_API_URL not set"}

    headers = {}
    if EXTRACTOR_API_KEY:
        headers["Authorization"] = f"Bearer {EXTRACTOR_API_KEY}"

    file_bytes = await attachment.read()
    data = aiohttp.FormData()
    data.add_field("file", file_bytes, filename=attachment.filename, content_type="application/octet-stream")

    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(EXTRACTOR_API_URL, data=data, headers=headers, timeout=timeout) as resp:
                text = await resp.text()
                ctype = resp.headers.get("Content-Type", "")
                if resp.status not in (200, 201):
                    return {"status": "error", "error": f"{resp.status} {text[:400]}"}
                if "application/json" in ctype or text.strip().startswith("{") or text.strip().startswith("["):
                    try:
                        j = json.loads(text)
                        return {"status": "ok", "json": j}
                    except Exception:
                        return {"status": "ok", "text": text}
                return {"status": "ok", "text": text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

# ─── Команда імпорту через сервіс ───────────────────────────────────────────
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]
    await ctx.send("🔄 Завантажую файл на сервіс для очищення...")

    res = await upload_to_service(attachment)
    if res.get("status") != "ok":
        # повідомити про помилку
        return await ctx.send(f"❌ Помилка сервісу: {res.get('error')}")

    method_label = "external"
    departments = None
    decoded_text = None

    # Якщо сервіс повернув JSON з departments або decoded_text
    if res.get("json"):
        j = res["json"]
        if isinstance(j, dict) and j.get("departments"):
            departments = j["departments"]
            method_label = "external:departments"
        elif isinstance(j, dict) and j.get("decoded_text"):
            decoded_text = j["decoded_text"]
            method_label = "external:decoded_text"
        else:
            # якщо JSON — спробуємо знайти decoded_text or departments keys
            if isinstance(j, dict):
                for k in ("decoded_text", "text", "content"):
                    if j.get(k):
                        decoded_text = j.get(k)
                        method_label = "external:json-text"
                        break
    elif res.get("text"):
        # сервіс повернув plain text або HTML
        text = res["text"]
        # спробуємо витягти <pre> якщо є
        m = re.search(r'<pre[^>]*>(.*?)</pre>', text, flags=re.DOTALL | re.IGNORECASE)
        if m:
            decoded_text = html.unescape(re.sub(r'<[^>]+>', '', m.group(1)))
            method_label = "external:html-pre"
        else:
            # якщо текст містить JSON-like structure
            if text.strip().startswith("{") or text.strip().startswith("["):
                try:
                    j2 = json.loads(text)
                    if isinstance(j2, dict) and j2.get("departments"):
                        departments = j2["departments"]
                        method_label = "external:json-text"
                    elif isinstance(j2, dict) and j2.get("decoded_text"):
                        decoded_text = j2["decoded_text"]
                        method_label = "external:json-text"
                except Exception:
                    decoded_text = re.sub(r'<[^>]+>', ' ', text)
                    method_label = "external:plain"
            else:
                decoded_text = re.sub(r'<[^>]+>', ' ', text)
                method_label = "external:plain"

    # Якщо сервіс поверив departments — відправляємо їх
    if departments:
        # Очистити і відправити ембеди
        sent = 0
        total_slots = 0
        for d in departments:
            title = d.get("section") or d.get("name") or "Відділення"
            slots = d.get("slots") or d.get("units") or []
            cleaned = []
            for s in slots:
                name = s.get("name") if isinstance(s, dict) else s
                cleaned.append(clean_slot_value(name))
            cleaned = [c for c in cleaned if c][:25]
            total_slots += len(cleaned)
            embed = discord.Embed(title=title, description="\n".join(f"{i+1}. {x}" for i,x in enumerate(cleaned)) or "— слотів не знайдено —", color=discord.Color.blurple())
            try:
                await ctx.send(embed=embed)
                sent += 1
            except Exception:
                pass
        await ctx.send(f"✅ Очищення завершено. Опубліковано відділень: {sent}. Знайдено слотів: {total_slots}. Метод: `{method_label}`.")
        return

    # Якщо сервіс повернув decoded_text — парсимо локально і відправляємо
    if decoded_text:
        groups = parse_mission_sqm_flexible(decoded_text)
        if not groups:
            # фолбек: знайти unitName/description/text
            units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"', decoded_text, flags=re.IGNORECASE | re.DOTALL)
            if not units:
                units = re.findall(r"(?:unitName|description|text)\s*=\s*'(.*?)'", decoded_text, flags=re.IGNORECASE | re.DOTALL)
            if not units:
                arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', decoded_text, flags=re.IGNORECASE | re.DOTALL)
                for a in arrs:
                    units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
            if units:
                groups = [("Відділення", [clean_slot_value(u) for u in units])]

        if not groups:
            preview = (decoded_text or "")[:2000]
            emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.red())
            emb.add_field(name="Метод обробки", value=method_label, inline=False)
            emb.add_field(name="Прев'ю", value=f"```{preview}```", inline=False)
            try:
                buf = io.BytesIO((decoded_text or "").encode("utf-8"))
                buf.seek(0)
                await ctx.send(embed=emb, file=discord.File(fp=buf, filename="decoded_mission_sqm.txt"))
            except Exception:
                await ctx.send(embed=emb)
            return

        sent = 0
        total_slots = 0
        for name, slots in groups:
            cleaned = [clean_slot_value(s) for s in slots][:25]
            total_slots += len(cleaned)
            embed = discord.Embed(title=name or "Відділення", description="\n".join(f"{i+1}. {s}" for i,s in enumerate(cleaned)) or "— слотів не знайдено —", color=discord.Color.blurple())
            try:
                await ctx.send(embed=embed)
                sent += 1
            except Exception:
                pass
        await ctx.send(f"✅ Очищення завершено. Опубліковано відділень: {sent}. Знайдено слотів: {total_slots}. Метод: `{method_label}`.")
        return

    # Якщо нічого не вдалося — повідомити
    await ctx.send("⚠️ Сервіс повернув непередбачуваний результат. Спробуйте інший сервіс або надішліть файл локально для діагностики.")
# --- Початок вставки: інтеграція з зовнішнім сервісом для очищення (.pbo/.sqm) ---
import os, json, aiohttp, html, re, io
from typing import Dict, Any, List, Tuple, Optional

EXTRACTOR_API_URL = os.getenv("EXTRACTOR_API_URL")  # наприклад https://fileproinfo.com/api/upload
EXTRACTOR_API_KEY = os.getenv("EXTRACTOR_API_KEY")  # опціонально

def extract_mission_from_pbo_bytes(pbo_bytes: bytes) -> Optional[bytes]:
    """Швидкий PBO->mission.sqm витяг через zip fallback (якщо всередині zip)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(pbo_bytes))
        for name in z.namelist():
            if name.lower().endswith("mission.sqm") or name.lower().endswith(".sqm"):
                return z.read(name)
    except Exception:
        pass
    return None

async def upload_to_service(attachment: discord.Attachment, timeout: int = 60) -> Dict[str, Any]:
    """
    Завантажує файл на EXTRACTOR_API_URL і повертає структуру:
      {'status':'ok','json':...} або {'status':'ok','text':...} або {'status':'error','error':...}
    """
    if not EXTRACTOR_API_URL:
        return {"status": "error", "error": "EXTRACTOR_API_URL not configured"}

    headers = {}
    if EXTRACTOR_API_KEY:
        headers["Authorization"] = f"Bearer {EXTRACTOR_API_KEY}"

    file_bytes = await attachment.read()
    data = aiohttp.FormData()
    data.add_field("file", file_bytes, filename=attachment.filename, content_type="application/octet-stream")

    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(EXTRACTOR_API_URL, data=data, headers=headers, timeout=timeout) as resp:
                text = await resp.text()
                ctype = resp.headers.get("Content-Type", "")
                if resp.status not in (200, 201):
                    return {"status": "error", "error": f"{resp.status} {text[:400]}"}
                if "application/json" in ctype or text.strip().startswith("{") or text.strip().startswith("["):
                    try:
                        j = json.loads(text)
                        return {"status": "ok", "json": j}
                    except Exception:
                        return {"status": "ok", "text": text}
                return {"status": "ok", "text": text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

# Команда: імпорт через зовнішній сервіс (фолбек на локальний парсер)
@bot.command(name="імпорт_sqm", aliases=["import_sqm"])
async def імпорт_sqm(ctx: commands.Context):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        return await ctx.send("❌ Команда доступна лише в адміністративному каналі.")
    if not ctx.message.attachments:
        return await ctx.send("❌ Прикріпіть файл .pbo або mission.sqm до повідомлення.")

    attachment = ctx.message.attachments[0]
    await ctx.send("🔄 Завантажую файл на сервіс для очищення...")

    res = await upload_to_service(attachment)
    if res.get("status") != "ok":
        # фолбек: локальна обробка (якщо є read_attachment_sqm_text)
        try:
            if 'read_attachment_sqm_text' in globals():
                text, method = await read_attachment_sqm_text(attachment)
                method_label = f"local:{method}"
            else:
                return await ctx.send(f"❌ Помилка сервісу: {res.get('error')}")
        except Exception as e:
            return await ctx.send(f"❌ Помилка сервісу і локального фолбеку: {e}")
    else:
        method_label = "external"
        decoded_text = None
        departments = None

        if res.get("json"):
            j = res["json"]
            # якщо сервіс повернув готові departments
            if isinstance(j, dict) and j.get("departments"):
                departments = j["departments"]
                method_label = "external:departments"
            # якщо сервіс повернув decoded_text
            elif isinstance(j, dict) and j.get("decoded_text"):
                decoded_text = j["decoded_text"]
                method_label = "external:decoded_text"
            else:
                # знайти можливі текстові поля
                for k in ("decoded_text", "text", "content", "result"):
                    if isinstance(j, dict) and j.get(k):
                        decoded_text = j.get(k)
                        method_label = "external:json-text"
                        break
                # якщо JSON — але не містить текст/відділення — збережемо як текст для парсингу
                if decoded_text is None and isinstance(j, (dict, list)):
                    decoded_text = json.dumps(j, ensure_ascii=False)
                    method_label = "external:json-dump"
        elif res.get("text"):
            txt = res["text"]
            # витягнути <pre> якщо є
            m = re.search(r'<pre[^>]*>(.*?)</pre>', txt, flags=re.DOTALL | re.IGNORECASE)
            if m:
                decoded_text = html.unescape(re.sub(r'<[^>]+>', '', m.group(1)))
                method_label = "external:html-pre"
            else:
                # plain text або HTML fallback
                decoded_text = re.sub(r'<[^>]+>', ' ', txt)
                method_label = "external:plain"

        # якщо сервіс повернув departments — відправляємо їх
        if departments:
            sent = 0
            total_slots = 0
            for d in departments:
                title = d.get("section") or d.get("name") or "Відділення"
                slots = d.get("slots") or d.get("units") or []
                cleaned = []
                for s in slots:
                    name = s.get("name") if isinstance(s, dict) else s
                    # використовуємо наявну clean_slot_value якщо є
                    if 'clean_slot_value' in globals():
                        cleaned.append(clean_slot_value(name))
                    else:
                        cleaned.append(str(name or "Слот"))
                cleaned = [c for c in cleaned if c][:25]
                total_slots += len(cleaned)
                embed = discord.Embed(title=title, description="\n".join(f"{i+1}. {x}" for i,x in enumerate(cleaned)) or "— слотів не знайдено —", color=discord.Color.blurple())
                try:
                    await ctx.send(embed=embed)
                    sent += 1
                except Exception:
                    pass
            await ctx.send(f"✅ Очищення завершено. Опубліковано відділень: {sent}. Знайдено слотів: {total_slots}. Метод: `{method_label}`.")
            return

        # якщо сервіс повернув текст — парсимо локально
        if decoded_text:
            # якщо текст виглядає як бінар/rap (має нульові символи або 'raP' на початку), спробуємо витягти сирі байти з attachment і обробити локально
            if ("\x00" in decoded_text) or decoded_text.strip().startswith("raP"):
                try:
                    raw_bytes = await attachment.read()
                    sqm_raw = extract_mission_from_pbo_bytes(raw_bytes) or raw_bytes
                    if 'rap_to_text_aggressive' in globals():
                        decoded_text = rap_to_text_aggressive(sqm_raw)
                        method_label += "+rap_fallback"
                except Exception:
                    pass

            # використовуємо наявний парсер parse_mission_sqm_flexible
            if 'parse_mission_sqm_flexible' in globals():
                groups = parse_mission_sqm_flexible(decoded_text)
            else:
                groups = []

            if not groups:
                # фолбек: знайти unitName/description/text
                units = re.findall(r'(?:unitName|description|text)\s*=\s*"(.*?)"', decoded_text, flags=re.IGNORECASE | re.DOTALL)
                if not units:
                    units = re.findall(r"(?:unitName|description|text)\s*=\s*'(.*?)'", decoded_text, flags=re.IGNORECASE | re.DOTALL)
                if not units:
                    arrs = re.findall(r'(?:unitName|description|text)\s*\[\s*\]\s*=\s*\{(.*?)\}', decoded_text, flags=re.IGNORECASE | re.DOTALL)
                    for a in arrs:
                        units += re.findall(r'[\'"](.+?)[\'"]', a, flags=re.DOTALL)
                if units:
                    groups = [("Відділення", [ (clean_slot_value(u) if 'clean_slot_value' in globals() else u) for u in units ])]

            if not groups:
                preview = (decoded_text or "")[:2000]
                emb = discord.Embed(title="ℹ️ Не знайдено відділень", color=discord.Color.red())
                emb.add_field(name="Метод обробки", value=method_label, inline=False)
                emb.add_field(name="Прев'ю", value=f"```{preview}```", inline=False)
                try:
                    buf = io.BytesIO((decoded_text or "").encode('utf-8'))
                    buf.seek(0)
                    await ctx.send(embed=emb, file=discord.File(fp=buf, filename="decoded_mission_sqm.txt"))
                except Exception:
                    await ctx.send(embed=emb)
                return

            # відправляємо ембеди з груп
            sent = 0
            total_slots = 0
            for name, slots in groups:
                cleaned = [ (clean_slot_value(s) if 'clean_slot_value' in globals() else s) for s in slots ][:25]
                total_slots += len(cleaned)
                embed = discord.Embed(title=name or "Відділення", description="\n".join(f"{i+1}. {s}" for i,s in enumerate(cleaned)) or "— слотів не знайдено —", color=discord.Color.blurple())
                try:
                    await ctx.send(embed=embed)
                    sent += 1
                except Exception:
                    pass
            await ctx.send(f"✅ Очищення завершено. Опубліковано відділень: {sent}. Знайдено слотів: {total_slots}. Метод: `{method_label}`.")
            return

    # Якщо нічого не повернуто
    await ctx.send("⚠️ Сервіс повернув непередбачуваний результат. Спробуйте інший сервіс або надішліть файл локально для діагностики.")
# --- Кінець вставки ---
# ─── on_ready ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[on_ready] {bot.user} (id: {bot.user.id})")
    try:
        commit = subprocess.getoutput("git rev-parse --short HEAD")
    except Exception:
        commit = "unknown"
    embed = discord.Embed(title="🔄 Бот перезапущено", description=f"📦 Commit: `{commit}`", color=discord.Color.green())
    for guild in bot.guilds:
        ch = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages, guild.text_channels)
        if ch:
            try:
                await ch.send(embed=embed)
            except:
                pass

# ─── Запуск ────────────────────────────────────────────────────────────────
if not TOKEN:
    print("DISCORD_TOKEN not set in environment")
else:
    bot.run(TOKEN)
