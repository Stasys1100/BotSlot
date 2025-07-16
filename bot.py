import os, re, subprocess, aiohttp, datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv
from keep_alive import keep_alive

keep_alive()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEPLOY_HOOK_URL = os.getenv("DEPLOY_HOOK_URL")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot("!", intents=intents)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
VTG_CHANNEL_ID = 1160843618433630228
ADMIN_CHANNEL_ID = 1395065909185478769

processed: set[int] = set()
sessions: dict[int, dict] = {}
claims: dict[tuple[int,int], list] = {}
req_counter = 0

TRIGGER = re.compile(r'^\s*(\d+)[\.:]\s*(.+)$')
MENT = re.compile(r'<@!?(?P<id>\d+)>')
DEF_TITLE = "Alpha 1-2 | 3. Prikaati 'Karhu' | Jalkaväen haara"

@tasks.loop(minutes=1)
async def vtg():
    now = datetime.datetime.now(KYIV_TZ)
    if now.weekday() in (4,6) and now.hour==19 and now.minute==30:
        ch = bot.get_channel(VTG_CHANNEL_ID)
        if ch: await ch.send("||@everyone||\n**Сбор VTG**")

def build_embed(s):
    e = discord.Embed(title=s["title"], color=discord.Color.blue())
    lines=[]
    for i,(ln,owner) in enumerate(zip(s["lines"],s["owners"])):
        pre=f"{i+1}. "
        if owner: lines.append(pre+ln+f" – Зайнято {owner.mention}")
        else:     lines.append(pre+ln)
    e.description="\n".join(lines)
    return e

class SlotButton(Button):
    def __init__(self, sid:int, idx:int):
        owner=sessions[sid]["owners"][idx]
        if owner is None:
            lbl,style=f"{idx+1}. Зайняти",discord.ButtonStyle.success
        else:
            lbl,style=f"{idx+1}. Відмовитись",discord.ButtonStyle.danger
        super().__init__(lbl, style, custom_id=f"slot-{sid}-{idx}")
        self.sid, self.idx = sid, idx

    async def callback(self, i:discord.Interaction):
        user=i.user
        s=sessions[self.sid]
        owner=s["owners"][self.idx]
        cid=s["channel_id"]

        # вільний → зайняти
        if owner is None:
            for o in sessions.values():
                if o["channel_id"]==cid and user in o["owners"]:
                    return await i.response.send_message(
                        "⚠️ Ви вже маєте слот в цій гілці.", ephemeral=True
                    )
            s["owners"][self.idx]=user
            return await i.response.edit_message(
                embed=build_embed(s), view=SlotView(self.sid)
            )

        # ваш слот → звільнити
        if owner==user:
            s["owners"][self.idx]=None
            return await i.response.edit_message(
                embed=build_embed(s), view=SlotView(self.sid)
            )

        # чужий → ефермерно «Претендувати»
        return await i.response.send_message(
            f"⚠️ Слот зайнято {owner.mention}.", 
            view=ClaimView(self.sid,self.idx), ephemeral=True
        )

class SlotView(View):
    def __init__(self,sid:int):
        super().__init__(timeout=None)
        for idx in range(len(sessions[sid]["lines"])):
            owner=sessions[sid]["owners"][idx]
            # додаємо кнопку лише якщо вільно чи ваше
            if owner is None or owner==bot.user or owner==owner:
                self.add_item(SlotButton(sid,idx))

class ClaimButton(Button):
    def __init__(self, sid:int, idx:int):
        super().__init__("❗ Претендувати", discord.ButtonStyle.primary,
                         custom_id=f"claim-{sid}-{idx}")
        self.sid,self.idx=sid,idx

    async def callback(self,i:discord.Interaction):
        user=i.user
        s=sessions[self.sid]
        # не претенд, якщо вже маєте слот
        for o in sessions.values():
            if o["channel_id"]==s["channel_id"] and user in o["owners"]:
                return await i.response.send_message(
                    "⚠️ Ви вже маєте слот.", ephemeral=True
                )
        key=(self.sid,self.idx)
        lst=claims.setdefault(key,[])
        if user in lst:
            return await i.response.send_message(
                "ℹ️ Вже подали заявку.", ephemeral=True
            )
        lst.append(user)
        await i.response.send_message("✅ Заявка надіслана.", ephemeral=True)

        global req_counter
        req_counter+=1
        emb=discord.Embed(
            title=f"📝 Заявка #{req_counter}",
            description=s["title"], color=discord.Color.orange()
        )
        emb.add_field("Слот #",str(self.idx+1),True)
        emb.add_field("Власник",
            s["owners"][self.idx].mention if s["owners"][self.idx] else "Вільний",True)
        emb.add_field("Кандидат",user.mention,False)

        ch=bot.get_channel(ADMIN_CHANNEL_ID)
        msg=await ch.send(embed=emb)
        await msg.edit(view=DecisionView(self.sid,self.idx,user.id,msg.id))

class ClaimView(View):
    def __init__(self,sid:int,idx:int):
        super().__init__(timeout=None)
        self.add_item(ClaimButton(sid,idx))

class DecisionModal(Modal):
    def __init__(self,sid,idx,uid,msgid,accept):
        super().__init__(title="Причина призначення" if accept else "Причина відмови")
        self.sid,self.idx,self.uid,self.msgid,self.accept=sid,idx,uid,msgid,accept
        self.reason=TextInput(label="Причина",style=discord.TextStyle.paragraph)
        self.add_item(self.reason)
    async def on_submit(self,i:discord.Interaction):
        s=sessions[self.sid]
        key=(self.sid,self.idx)
        user=await bot.fetch_user(self.uid)
        old=s["owners"][self.idx]
        reason=self.reason.value

        if self.accept:
            s["owners"][self.idx]=user
            claims.pop(key,None)
        else:
            lst=claims.get(key,[])
            if user in lst: lst.remove(user)

        ch=bot.get_channel(s["channel_id"])
        if ch:
            msg=await ch.fetch_message(self.sid)
            await msg.edit(embed=build_embed(s),view=SlotView(self.sid))

        try:
            if self.accept:
                await user.send(f"✅ Призначено на слот #{self.idx+1}.\nПричина: {reason}")
                if old and old!=user:
                    await old.send(f"⚠️ Ваш слот #{self.idx+1} віддано.\nПричина: {reason}")
            else:
                await user.send(f"❌ Заявка відхилена.\nПричина: {reason}")
        except: pass

        adm=bot.get_channel(ADMIN_CHANNEL_ID)
        await adm.fetch_message(self.msgid)
        await adm.delete()

        await i.response.send_message("✔️ Готово.",ephemeral=True)

class DecisionView(View):
    def __init__(self,sid,idx,uid,msgid):
        super().__init__(timeout=None)
        self.add_item(Button("✅ Призначити",discord.ButtonStyle.success,
                        custom_id=f"dec-accept-{sid}-{idx}-{uid}-{msgid}"))
        self.add_item(Button("❌ Відхилити", discord.ButtonStyle.danger,
                        custom_id=f"dec-deny-{sid}-{idx}-{uid}-{msgid}"))

@bot.event
async def on_ready():
    print("Ready",bot.user)
    commit=subprocess.getoutput("git rev-parse --short HEAD")
    emb=discord.Embed("🔄 Бот перезапущено",f"📦 `{commit}`",discord.Color.green())
    for g in bot.guilds:
        ch=discord.utils.find(
            lambda c:isinstance(c,discord.TextChannel)
            and c.permissions_for(g.me).send_messages,
            g.text_channels
        )
        if ch: await ch.send(embed=emb)
    if not vtg.is_running(): vtg.start()

@bot.event
async def on_message(msg):
    if msg.author.bot or msg.id in processed: return
    if "запис слоти" in msg.content.lower():
        processed.add(msg.id)
        header,slots,owners=None,[],[]
        for line in msg.content.splitlines():
            t=line.strip()
            if not t or "запис слоти" in t.lower(): continue
            m=TRIGGER.match(t)
            if m:
                o=next((u for u in msg.mentions
                         if f"<@{u.id}>" in t or f"<@!{u.id}>" in t),None)
                clean=MENT.sub("",m.group(2)).strip()
                slots.append(clean); owners.append(o)
            elif header is None:
                header=t
        slots,owners=slots[:25],owners[:len(slots)]
        s={"title":header or DEF_TITLE,
           "lines":slots,"owners":owners,
           "channel_id":msg.channel.id}
        e=build_embed(s)
        sent=await msg.channel.send(embed=e)
        sessions[sent.id]=s
        await sent.edit(view=SlotView(sent.id))
    await bot.process_commands(msg)

bot.run(TOKEN)