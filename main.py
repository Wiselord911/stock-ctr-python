# main.py
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# -------- ENV --------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

CHAN_ALL_ITEMS = os.getenv("CHAN_ALL_ITEMS", "รายการทั้งหมด")
CHAN_EDIT_LOG = os.getenv("CHAN_EDIT_LOG", "ประวัติการแก้ไข")
CHAN_EDIT_ADD_LOG = os.getenv("CHAN_EDIT_ADD_LOG", "ประวัติการแก้ไข-เพิ่มรายการ")
CHAN_STOCK_LIST = os.getenv("CHAN_STOCK_LIST", "รายการสต็อคทั้งหมด")
CHAN_RECEIVE = os.getenv("CHAN_RECEIVE", "รายการรับเข้า")
CHAN_ISSUE = os.getenv("CHAN_ISSUE", "รายการเบิก")
CHAN_STOCK_UPDATES = os.getenv("CHAN_STOCK_UPDATES", "อัพเดทสต็อค")

DB_PATH = os.getenv("DB_PATH") or os.path.join(os.getcwd(), "stock.db")

if not DISCORD_TOKEN or not GUILD_ID:
    raise SystemExit("โปรดตั้งค่า DISCORD_TOKEN และ GUILD_ID ใน .env")

# -------- DB --------
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

def init_db():
    cur = conn.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      created_by TEXT,
      created_at INTEGER,
      updated_at INTEGER,
      active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS stock_batches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      item_id INTEGER NOT NULL,
      received_qty INTEGER NOT NULL,
      remaining_qty INTEGER NOT NULL,
      expiry_date INTEGER, -- epoch ms or NULL
      received_by TEXT,
      received_at INTEGER,
      FOREIGN KEY(item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      type TEXT NOT NULL, -- ADD, EDIT, DELETE, RECEIVE, ISSUE
      item_id INTEGER,
      qty INTEGER,
      batch_id INTEGER,
      expiry_date INTEGER,
      by_user TEXT,
      note TEXT,
      created_at INTEGER,
      FOREIGN KEY(item_id) REFERENCES items(id),
      FOREIGN KEY(batch_id) REFERENCES stock_batches(id)
    );
    """)
    conn.commit()

def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)

def find_item_by_name(name: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM items WHERE name = ? AND active = 1", (name.strip(),))
    return cur.fetchone()

def list_items_like(prefix: str, limit: int = 25) -> List[str]:
    cur = conn.execute(
        "SELECT name FROM items WHERE active = 1 AND name LIKE ? ORDER BY name LIMIT ?",
        (f"{prefix}%", limit),
    )
    return [r["name"] for r in cur.fetchall()]

def list_all_items() -> List[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM items WHERE active = 1 ORDER BY name")
    return cur.fetchall()

def calc_stock_summary(item_id: int) -> Tuple[int, Optional[int]]:
    total = conn.execute(
        "SELECT COALESCE(SUM(remaining_qty),0) AS total FROM stock_batches WHERE item_id = ?",
        (item_id,),
    ).fetchone()["total"]
    next_row = conn.execute(
        """SELECT * FROM stock_batches
           WHERE item_id = ? AND remaining_qty > 0
           ORDER BY COALESCE(expiry_date, 253402300799000) ASC, received_at ASC
           LIMIT 1""",
        (item_id,),
    ).fetchone()
    next_exp = next_row["expiry_date"] if next_row else None
    return total, next_exp

def parse_expiry(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    # DD/MM/YYYY
    try:
        dt = datetime.strptime(s.strip(), "%d/%m/%Y")
        # set noon to avoid timezone shift
        dt = dt.replace(hour=12, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def fmt_date(ts: Optional[int]) -> str:
    if not ts:
        return "ไม่พบข้อมูล"
    return datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%d/%m/%Y %H:%M")

def fmt_date_short(ts: Optional[int]) -> str:
    if not ts:
        return "ไม่พบข้อมูล"
    return datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%d/%m/%Y")

# -------- Discord Bot --------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- Utilities ----------
async def ensure_channels(guild: discord.Guild):
    need = [
        CHAN_ALL_ITEMS, CHAN_EDIT_LOG, CHAN_EDIT_ADD_LOG,
        CHAN_STOCK_LIST, CHAN_RECEIVE, CHAN_ISSUE, CHAN_STOCK_UPDATES
    ]
    for name in need:
        if discord.utils.get(guild.text_channels, name=name) is None:
            try:
                await guild.create_text_channel(name, reason="STOCK CTR auto-setup")
                print(f"สร้างห้อง: {name}")
            except Exception as e:
                print(f"⚠️ สร้างห้อง {name} ไม่ได้: {e}")

async def purge_bot_messages(channel: discord.TextChannel):
    """ลบเฉพาะข้อความที่ 'บอท' เคยโพสต์ในห้องนั้น (ปลอดภัยกับข้อจำกัด 14 วัน)"""
    try:
        async for msg in channel.history(limit=200):
            if msg.author.id == bot.user.id:
                try:
                    await msg.delete()
                except:
                    pass
    except Exception as e:
        print("purge_bot_messages:", e)

async def post_all_items_current(guild: discord.Guild):
    ch: discord.TextChannel = discord.utils.get(guild.text_channels, name=CHAN_ALL_ITEMS)
    if not ch:
        return
    await purge_bot_messages(ch)

    items = list_all_items()
    lines = [f"• {r['name']}" for r in items]
    header = f"**รายการทั้งหมด ({len(lines)})**\n"

    buf = header
    chunks: List[str] = []
    for line in lines:
        if len(buf) + len(line) + 1 > 1900:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        chunks.append(buf)

    for c in chunks:
        await ch.send(c)

def everyone_mention() -> str:
    return "@everyone"

# -------- Views (Confirm Dialog) --------
class ConfirmView(discord.ui.View):
    def __init__(self, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None

    @discord.ui.button(label="ยืนยัน", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

async def ask_confirm(inter: discord.Interaction, prompt: str) -> bool:
    v = ConfirmView()
    await inter.response.send_message(prompt, view=v, ephemeral=True)
    await v.wait()
    if v.value is None:
        await inter.followup.send("หมดเวลา ยกเลิกแล้ว", ephemeral=True)
        return False
    return v.value

# -------- Events --------
@bot.event
async def on_ready():
    init_db()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await ensure_channels(guild)
        try:
            await tree.sync(guild=discord.Object(id=GUILD_ID))
            print("✅ Registered guild commands")
        except Exception as e:
            print("Sync error:", e)

# -------- Autocomplete --------
async def item_autocomplete(interaction: discord.Interaction, current: str):
    names = list_items_like(current or "")
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]

# -------- Commands --------
@tree.command(name="additem", description="เพิ่มรายการสินค้า", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(name="ชื่อสินค้า")
async def additem(inter: discord.Interaction, name: str):
    guild = inter.guild
    if not guild:
        return

    if not await ask_confirm(inter, f"ยืนยันที่จะบันทึกรายการ **{name}** ใช่หรือไม่?"):
        return

    try:
        conn.execute(
            "INSERT INTO items (name, created_by, created_at, updated_at, active) VALUES (?,?,?,?,1)",
            (name.strip(), str(inter.user), now_ms(), now_ms())
        )
        conn.commit()
        item = find_item_by_name(name)
        conn.execute(
            "INSERT INTO transactions (type, item_id, by_user, created_at, note) VALUES (?,?,?,?,?)",
            ("ADD", item["id"], str(inter.user), now_ms(), "เพิ่มรายการสินค้า")
        )
        conn.commit()

        await inter.followup.send(f"✅ เพิ่มรายการ **{name}** สำเร็จ", ephemeral=True)
        await post_all_items_current(guild)

        ch_edit_add = discord.utils.get(guild.text_channels, name=CHAN_EDIT_ADD_LOG)
        if ch_edit_add:
            await ch_edit_add.send(f"เพิ่มรายการ **{name}** โดย {inter.user.mention}")

    except sqlite3.IntegrityError:
        await inter.followup.send("ชื่อสินค้านี้มีอยู่แล้ว", ephemeral=True)

@tree.command(name="edititem", description="แก้ไขชื่อรายการสินค้า", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(old_name="ชื่อเดิม", new_name="ชื่อใหม่")
async def edititem(inter: discord.Interaction, old_name: str, new_name: str):
    guild = inter.guild
    if not guild:
        return
    item = find_item_by_name(old_name)
    if not item:
        await inter.response.send_message("ไม่พบรายการเดิม", ephemeral=True)
        return

    if not await ask_confirm(inter, f"คุณยืนยันที่จะแก้ไขรายการ\n**{old_name} ➜ {new_name}** ใช่หรือไม่?"):
        return

    conn.execute("UPDATE items SET name = ?, updated_at = ? WHERE id = ?",
                 (new_name.strip(), now_ms(), item["id"]))
    conn.execute(
        "INSERT INTO transactions (type, item_id, by_user, created_at, note) VALUES (?,?,?,?,?)",
        ("EDIT", item["id"], str(inter.user), now_ms(), f"{old_name} ➜ {new_name}")
    )
    conn.commit()

    await inter.followup.send(f"✏️ แก้ไขชื่อเรียบร้อย: **{old_name} ➜ {new_name}**", ephemeral=True)
    await post_all_items_current(guild)

    ch_edit = discord.utils.get(guild.text_channels, name=CHAN_EDIT_LOG)
    ch_edit_add = discord.utils.get(guild.text_channels, name=CHAN_EDIT_ADD_LOG)
    if ch_edit:
        await ch_edit.send(f"แก้ไขรายการ: **{old_name} ➜ {new_name}** โดย {inter.user.mention}")
    if ch_edit_add:
        await ch_edit_add.send(f"มีการแก้ไข/เพิ่มรายการ: **{old_name} ➜ {new_name}** โดย {inter.user.mention}")

@tree.command(name="deleteitem", description="ลบรายการสินค้า", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(name="ชื่อสินค้า")
async def deleteitem(inter: discord.Interaction, name: str):
    guild = inter.guild
    if not guild:
        return
    item = find_item_by_name(name)
    if not item:
        await inter.response.send_message("ไม่พบรายการ", ephemeral=True)
        return

    if not await ask_confirm(inter, f"ยืนยันที่จะลบรายการ **{name}** ใช่หรือไม่?"):
        return

    conn.execute("UPDATE items SET active = 0, updated_at = ? WHERE id = ?", (now_ms(), item["id"]))
    conn.execute(
        "INSERT INTO transactions (type, item_id, by_user, created_at, note) VALUES (?,?,?,?,?)",
        ("DELETE", item["id"], str(inter.user), now_ms(), "ลบรายการสินค้า")
    )
    conn.commit()

    await inter.followup.send(f"🗑️ ลบรายการ **{name}** แล้ว", ephemeral=True)
    await post_all_items_current(guild)

    ch_edit = discord.utils.get(guild.text_channels, name=CHAN_EDIT_LOG)
    ch_edit_add = discord.utils.get(guild.text_channels, name=CHAN_EDIT_ADD_LOG)
    if ch_edit:
        await ch_edit.send(f"ลบรายการ: **{name}** โดย {inter.user.mention}")
    if ch_edit_add:
        await ch_edit_add.send(f"มีการแก้ไข/เพิ่มรายการ: ลบ **{name}** โดย {inter.user.mention}")

@tree.command(name="stock", description="แสดงรายการสต็อคทั้งหมด (Embed สีฟ้า)", guild=discord.Object(id=GUILD_ID))
async def stock(inter: discord.Interaction):
    items = list_all_items()
    if not items:
        await inter.response.send_message("ยังไม่มีรายการสินค้า", ephemeral=True)
        return

    lines = []
    for it in items:
        total, next_exp = calc_stock_summary(it["id"])
        lines.append(f"**{it['name']}** — คงเหลือ: {total} | หมดอายุถัดไป: {fmt_date_short(next_exp) if next_exp else 'ไม่พบข้อมูล'}")

    embed = discord.Embed(
        title="รายการสต็อคทั้งหมด",
        description="\n".join(lines),
        color=0x3BA3F7,
    )
    embed.set_footer(text=f"อัปเดต: {fmt_date(now_ms())}")
    await inter.response.send_message(embed=embed, ephemeral=True)

    ch_stock = discord.utils.get(inter.guild.text_channels, name=CHAN_STOCK_LIST)
    if ch_stock:
        await ch_stock.send(embed=embed)

@tree.command(name="receive", description="รับเข้าสินค้า", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="ชื่อสินค้า", qty="จำนวนรับเข้า", expiry="วันหมดอายุ (DD/MM/YYYY) — ถ้าไม่ใส่ถือว่าไม่มี")
async def receive(inter: discord.Interaction, item: str, qty: app_commands.Range[int, 1, None], expiry: Optional[str] = None):
    guild = inter.guild
    if not guild:
        return
    row = find_item_by_name(item)
    if not row:
        await inter.response.send_message("ไม่พบรายการสินค้า", ephemeral=True)
        return
    exp_ms = parse_expiry(expiry)

    info = conn.execute(
        "INSERT INTO stock_batches (item_id, received_qty, remaining_qty, expiry_date, received_by, received_at) VALUES (?,?,?,?,?,?)",
        (row["id"], qty, qty, exp_ms, str(inter.user), now_ms())
    )
    conn.execute(
        "INSERT INTO transactions (type, item_id, qty, batch_id, expiry_date, by_user, created_at, note) VALUES (?,?,?,?,?,?,?,?)",
        ("RECEIVE", row["id"], qty, info.lastrowid, exp_ms, str(inter.user), now_ms(), "รับเข้า")
    )
    conn.commit()

    total, _ = calc_stock_summary(row["id"])
    embed = discord.Embed(
        title="รับเข้าสินค้า",
        description=(f"รายการ: **{row['name']}**\n"
                     f"จำนวน: **{qty}**\n"
                     f"วันหมดอายุ: **{fmt_date_short(exp_ms) if exp_ms else 'ไม่พบข้อมูล'}**\n"
                     f"ผู้บันทึก: {inter.user.mention}\n"
                     f"คงเหลือ: **{total}**"),
        color=0x22C55E
    )

    ch_receive = discord.utils.get(guild.text_channels, name=CHAN_RECEIVE)
    if ch_receive:
        await ch_receive.send(content=everyone_mention(),
                              allowed_mentions=discord.AllowedMentions(everyone=True),
                              embed=embed)

    # ส่งอัปเดตคงเหลือไปห้องอัพเดทสต็อคเท่านั้น
    ch_updates = discord.utils.get(guild.text_channels, name=CHAN_STOCK_UPDATES)
    if ch_updates:
        upd = discord.Embed(title="อัปเดตสต็อค",
                            description=f"**{row['name']}** คงเหลือ: **{total}**",
                            color=0x3BA3F7)
        await ch_updates.send(embed=upd)

    await inter.response.send_message("✅ รับเข้าเรียบร้อย", embed=embed, ephemeral=True)

@tree.command(name="issue", description="เบิกออกสินค้า", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="ชื่อสินค้า", qty="จำนวนเบิกออก")
async def issue(inter: discord.Interaction, item: str, qty: app_commands.Range[int, 1, None]):
    guild = inter.guild
    if not guild:
        return
    row = find_item_by_name(item)
    if not row:
        await inter.response.send_message("ไม่พบรายการสินค้า", ephemeral=True)
        return

    batches = conn.execute(
        """SELECT * FROM stock_batches
           WHERE item_id = ? AND remaining_qty > 0
           ORDER BY COALESCE(expiry_date, 253402300799000) ASC, received_at ASC""",
        (row["id"],)
    ).fetchall()

    remaining = qty
    used = []
    for b in batches:
        if remaining <= 0:
            break
        take = min(b["remaining_qty"], remaining)
        if take <= 0:
            continue
        conn.execute("UPDATE stock_batches SET remaining_qty = remaining_qty - ? WHERE id = ?", (take, b["id"]))
        conn.execute(
            "INSERT INTO transactions (type, item_id, qty, batch_id, expiry_date, by_user, created_at, note) VALUES (?,?,?,?,?,?,?,?)",
            ("ISSUE", row["id"], take, b["id"], b["expiry_date"], str(inter.user), now_ms(), "เบิกออก")
        )
        used.append((b["id"], take, b["expiry_date"]))
        remaining -= take

    if remaining > 0:
        conn.rollback()
        await inter.response.send_message("❌ สต็อคไม่พอ", ephemeral=True)
        return

    conn.commit()
    total, _ = calc_stock_summary(row["id"])
    detail = "\n".join([f"ล็อต #{bid} — {take} (หมดอายุ: {fmt_date_short(exp) if exp else 'ไม่พบข้อมูล'})" for bid, take, exp in used])

    embed = discord.Embed(
        title="เบิกออกสินค้า",
        description=(f"รายการ: **{row['name']}**\n"
                     f"จำนวนรวม: **{qty}**\n"
                     f"ตัดล็อต:\n{detail}\n"
                     f"ผู้บันทึก: {inter.user.mention}\n"
                     f"คงเหลือ: **{total}**"),
        color=0xEF4444
    )

    ch_issue = discord.utils.get(guild.text_channels, name=CHAN_ISSUE)
    if ch_issue:
        await ch_issue.send(content=everyone_mention(),
                            allowed_mentions=discord.AllowedMentions(everyone=True),
                            embed=embed)

    # อัปเดตคงเหลือไปห้องอัพเดทสต็อค
    ch_updates = discord.utils.get(guild.text_channels, name=CHAN_STOCK_UPDATES)
    if ch_updates:
        upd = discord.Embed(title="อัปเดตสต็อค",
                            description=f"**{row['name']}** คงเหลือ: **{total}**",
                            color=0x3BA3F7)
        await ch_updates.send(embed=upd)

    await inter.response.send_message("✅ เบิกออกเรียบร้อย", embed=embed, ephemeral=True)

# ---------- Export CSV/PDF ----------
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

def export_rows():
    cur = conn.execute(
        """SELECT i.name,
                  COALESCE(SUM(b.remaining_qty),0) AS total,
                  MIN(COALESCE(b.expiry_date, 253402300799000)) AS next_expiry
           FROM items i
           LEFT JOIN stock_batches b ON b.item_id = i.id
           WHERE i.active = 1
           GROUP BY i.id
           ORDER BY i.name"""
    )
    return cur.fetchall()

@tree.command(name="export", description="ส่งออกข้อมูลสต็อค (CSV/PDF)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(format="csv หรือ pdf")
@app_commands.choices(format=[
    app_commands.Choice(name="csv", value="csv"),
    app_commands.Choice(name="pdf", value="pdf"),
])
async def export_cmd(inter: discord.Interaction, format: app_commands.Choice[str]):
    rows = export_rows()
    if format.value == "csv":
        path = os.path.join(os.getcwd(), f"export_stock_{int(datetime.now().timestamp())}.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("name,total,next_expiry\n")
            for r in rows:
                exp = "" if (r["next_expiry"] is None or r["next_expiry"] >= 253402300799000) else \
                    datetime.utcfromtimestamp(r["next_expiry"]/1000).strftime("%Y-%m-%d")
                f.write(f"{r['name']},{r['total']},{exp}\n")
        await inter.response.send_message(file=discord.File(path), ephemeral=True)
    else:
        path = os.path.join(os.getcwd(), f"export_stock_{int(datetime.now().timestamp())}.pdf")
        c = canvas.Canvas(path, pagesize=A4)
        width, height = A4
        y = height - 40
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(width/2, y, "STOCK CTR — รายการสต็อค")
        y -= 30
        c.setFont("Helvetica", 11)
        for r in rows:
            exp = "ไม่พบข้อมูล"
            if r["next_expiry"] is not None and r["next_expiry"] < 253402300799000:
                exp = datetime.utcfromtimestamp(r["next_expiry"]/1000).strftime("%d/%m/%Y")
            line = f"{r['name']} — คงเหลือ: {r['total']} — หมดอายุถัดไป: {exp}"
            if y < 40:
                c.showPage()
                y = height - 40
            c.drawString(40, y, line)
            y -= 18
        c.save()
        await inter.response.send_message(file=discord.File(path), ephemeral=True)

# -------- Run --------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
