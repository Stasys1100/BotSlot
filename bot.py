import os
import discord
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}  # session_id: {title, lines, owners, channel_id}

def build_embed(session):
    embed = discord.Embed(title=session["title"], color=0x2ecc71)
    for i, line in enumerate(session["lines"]):
        owner = session["owners"][i]
        name = owner.mention if owner else "—"
        embed.add_field(name=f"Слот #{i+1}", value=f"{line}\n**{name}**", inline=False)
    return embed

class SlotButton(Button):
    def __init__(self, session_id: int, index: int):
        super().__init__(label=str(index+1), style=discord.ButtonStyle.primary)
        self.session_id = session_id
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        session = sessions[self.session_id]
        user = interaction.user

        if session["owners"][self.index] == user:
            return await interaction.response.send_message("⚠️ Ви вже записані на цей слот.", ephemeral=True)
        if session["owners"][self.index] is not None:
            return await interaction.response.send_message("❌ Слот вже зайнятий.", ephemeral=True)

        session["owners"][self.index] = user
        msg = await interaction.channel.fetch_message(self.session_id)
        await msg.edit(embed=build_embed(session), view=SlotView(self.session_id))
        await interaction.response.send_message(f"✅ Ви записані на слот #{self.index+1}.", ephemeral=True)

class SlotView(View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        for i in range(len(sessions[session_id]["lines"])):
            self.add_item(SlotButton(session_id, i))

@bot.command(name="створити")
async def створити(ctx: commands.Context, *, title: str):
    lines = [f"Слот {i+1}" for i in range(5)]
    owners = [None] * len(lines)
    embed = discord.Embed(title=title, color=0x3498db)
    for i, line in enumerate(lines):
        embed.add_field(name=f"Слот #{i+1}", value=f"{line}\n—", inline=False)
    msg = await ctx.send(embed=embed, view=SlotView(ctx.message.id))
    sessions[msg.id] = {
        "title": title,
        "lines": lines,
        "owners": owners,
        "channel_id": ctx.channel.id
    }

@bot.command(name="зняти")
async def зняти(ctx: commands.Context, session_msg_id: int):
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send("❌ Сесія не знайдена.")
    user = ctx.author
    for i, owner in enumerate(session["owners"]):
        if owner == user:
            session["owners"][i] = None
            msg = await ctx.channel.fetch_message(session_msg_id)
            await msg.edit(embed=build_embed(session), view=SlotView(session_msg_id))
            return await ctx.send(f"✅ Ви зняті зі слота #{i+1}.")
    await ctx.send("⚠️ Ви не записані на жоден слот.")

@bot.command(name="оновити")
async def оновити(ctx: commands.Context, session_msg_id: int):
    session = sessions.get(session_msg_id)
    if not session:
        return await ctx.send("❌ Сесія не знайдена.")
    msg = await ctx.channel.fetch_message(session_msg_id)
    await msg.edit(embed=build_embed(session), view=SlotView(session_msg_id))
    await ctx.send("🔄 Повідомлення оновлено.")

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
                f"⚠️ Слот #{self.idx+1} вже зайнятий {sess['owners'][self.idx].mention}.", ephemeral=True
            )

        sess["owners"][self.idx] = user

        ch = bot.get_channel(sess["channel_id"])
        try:
            msg = await ch.fetch_message(self.sid)
            await msg.edit(embed=build_embed(sess), view=SlotView(self.sid))
        except: pass

        try:
            await user.send(
                f"✅ Вас записано на слот #{self.idx+1} у «{sess['title']}».\nПричина: {reason}"
            )
        except: pass

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
        for idx in range(len(sessions[sid]["lines"])):
            self.add_item(AssignSlotButton(sid, idx, uid))

bot.run(TOKEN)