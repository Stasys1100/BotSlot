import discord
from discord.ext import commands
from discord.ui import View, Button
import re, os
from dotenv import load_dotenv

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_DIGIT_EMOJI = {str(i): f"{i}️⃣" for i in range(1, 21)}
def number_to_emoji(n: int): return _DIGIT_EMOJI.get(str(n), f"{n}️⃣")

class PaginatedSlotView(View):
    def __init__(self, embed, slots, original_lines):
        super().__init__(timeout=None)
        self.embed = embed
        self.slots = slots
        self.original_lines = original_lines
        self.claimed = {}
        self.user_slot_map = {}
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        for idx, num, text in self.slots:
            if idx in self.claimed: self.add_item(SlotTakenButton(num))
            else: self.add_item(SlotButton(idx, num, text, self.embed))
        for uid, idx in self.user_slot_map.items():
            original = self.original_lines[idx]
            self.add_item(CancelButton(idx, original, self.embed, uid))

class SlotButton(Button):
    def __init__(self, idx, num, text, embed):
        super().__init__(label=f"{number_to_emoji(num)} Вільний", style=discord.ButtonStyle.green)
        self.idx, self.text, self.embed = idx, text, embed

    async def callback(self, inter):
        view = self.view
        uid = inter.user.id
        if uid in view.user_slot_map:
            await inter.response.send_message("❌ Ви вже зайняли слот.", ephemeral=True); return
        lines = self.embed.description.split("\n")
        lines[self.idx] = f"{self.text} — Зайнято ({inter.user.mention})"
        self.embed.description = "\n".join(lines)
        view.claimed[self.idx] = uid
        view.user_slot_map[uid] = self.idx
        view.original_lines[self.idx] = self.text
        view.update_buttons()
        await inter.response.edit_message(embed=self.embed, view=view)

class SlotTakenButton(Button):
    def __init__(self, num):
        super().__init__(label=f"{number_to_emoji(num)} Зайнято", style=discord.ButtonStyle.gray, disabled=True)

class CancelButton(Button):
    def __init__(self, idx, original, embed, uid):
        super().__init__(label="❌ Відмовитись", style=discord.ButtonStyle.red)
        self.idx, self.original, self.embed, self.uid = idx, original, embed, uid

    async def callback(self, inter):
        if inter.user.id != self.uid:
            await inter.response.send_message("❌ Це не ваш слот!", ephemeral=True); return
        lines = self.embed.description.split("\n")
        lines[self.idx] = self.original
        self.embed.description = "\n".join(lines)
        view = self.view
        view.claimed.pop(self.idx, None)
        view.user_slot_map.pop(self.uid, None)
        view.update_buttons()
        await inter.response.edit_message(embed=self.embed, view=view)

async def build_slots(message):
    lines = message.content.split("\n")[1:]
    blocks, current = [], []
    for line in lines:
        if not line.strip(): continue
        if not re.match(r"^\s*\d+[.:]", line):
            if current: blocks.append(current); current = []
        current.append(line)
    if current: blocks.append(current)

    all_lines, all_slots, seen = [], [], set()
    for block_index, block in enumerate(blocks):
        header = next((line.strip() for line in block if not re.match(r"^\s*\d+[.:]", line)), "Unnamed Block")
        for i, line in enumerate(block):
            all_lines.append(line)
            m = re.match(r"^\s*(\d+)[.:]\s*(.*)", line)
            if m:
                num = int(m.group(1))
                clean = re.sub(r"[–—-]\s*Зайнято.*$", "", line).strip()
                norm = re.sub(r"\s+", " ", clean.lower())
                context_key = f"block{block_index}|{norm}"
                if "зайнято" in line.lower() or context_key in seen: continue
                seen.add(context_key)
                all_slots.append((len(all_lines)-1, num, clean))

    embed = discord.Embed(title="📋 Слоти", description="\n".join(all_lines), color=0x00ff00)
    if not all_slots:
        await message.channel.send(embed=embed, content="⚠️ Немає вільних слотів.")
        return
    view = PaginatedSlotView(embed, all_slots, all_lines)
    await message.channel.send(embed=embed, view=view)

@bot.event
async def on_message(message):
    if message.content.lower().startswith("запис слоти"):
        await build_slots(message)
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
    if after.content.lower().startswith("запис слоти"):
        await build_slots(after)

@bot.event
async def on_ready():
    print(f"✅ Бот активний як {bot.user}")

bot.run(os.getenv("DISCORD_TOKEN"))