import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import uuid
import datetime
import requests

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SHEETS_TOKEN      = os.environ.get("GOOGLESHEETS_ACCESS_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1420690312531017850
STATUS_MSG_FILE   = "status_message_id.txt"

SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:M"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:M:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

# Column indices
COL_USER_ID     = 0
COL_USERNAME    = 1
COL_ISSUED_BY   = 2
COL_REASON      = 3
COL_RESTRICTION = 4
COL_START_DATE  = 5
COL_END_DATE    = 6
COL_INCIDENT_ID = 7
COL_TIMESTAMP   = 8
COL_ACTIVE      = 9
COL_REVOKED     = 10
COL_REVOKED_BY  = 11
COL_SOURCE      = 12

# ── Sheets helpers ─────────────────────────────────────────────────────────────

def sheets_headers():
    return {"Authorization": f"Bearer {SHEETS_TOKEN}", "Content-Type": "application/json"}

def pad(row, length=13):
    return list(row) + [""] * (length - len(row))

def read_all_rows():
    try:
        resp = requests.get(SHEET_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        return rows[1:] if len(rows) > 1 else []
    except Exception as e:
        print(f"[Sheets] Read error: {e}")
        return []

def append_row(row):
    try:
        requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
    except Exception as e:
        print(f"[Sheets] Append error: {e}")

def update_row(row_index, row):
    try:
        range_str = f"{SHEET_NAME}!A{row_index}:M{row_index}"
        url = f"{SHEET_UPDATE_BASE}{range_str}?valueInputOption=RAW"
        requests.put(url, headers=sheets_headers(), json={"values": [row]}, timeout=10)
    except Exception as e:
        print(f"[Sheets] Update error: {e}")

def find_warning_by_id(warning_id):
    for i, row in enumerate(read_all_rows()):
        row = pad(row)
        if row[COL_INCIDENT_ID].strip().upper() == warning_id.strip().upper():
            return row, i + 2
    return None, None

def get_user_warnings(user_id):
    results = []
    for i, row in enumerate(read_all_rows()):
        row = pad(row)
        if row[COL_USER_ID].strip() == str(user_id):
            results.append((row, i + 2))
    return results

# ── Status Page ────────────────────────────────────────────────────────────────

STATUS_EMOJI = {
    "operational":          "✅",
    "degraded_performance": "🟨",
    "partial_outage":       "🟧",
    "major_outage":         "🔴",
    "under_maintenance":    "🔵",
    "unknown":              "⬜",
}

STATUS_COLOR = {
    "none":        discord.Color.green(),
    "minor":       discord.Color.yellow(),
    "major":       discord.Color.red(),
    "critical":    discord.Color.red(),
    "maintenance": discord.Color.blue(),
    "unknown":     discord.Color.light_grey(),
}

def status_label(s):
    return s.replace("_", " ").title()

def build_status_embed(data):
    page       = data.get("page", {})
    status     = data.get("status", {})
    components = data.get("components", [])
    incidents  = data.get("incidents", [])
    maints     = data.get("scheduled_maintenances", [])
    indicator  = status.get("indicator", "unknown")
    color      = STATUS_COLOR.get(indicator, discord.Color.light_grey())

    embed = discord.Embed(
        title=f"📡 {page.get('name', 'Status Page')} — Live Status",
        url=page.get("url", "https://bwr7s.statuspage.io"),
        description=f"**Overall:** {STATUS_EMOJI.get(indicator, '⬜')} {status.get('description', 'Unknown')}",
        color=color,
        timestamp=datetime.datetime.utcnow()
    )

    visible = [c for c in components if not c.get("group", False)]
    if visible:
        lines = [f"{STATUS_EMOJI.get(c.get('status','unknown'), '⬜')} **{c['name']}** — {status_label(c.get('status','unknown'))}" for c in visible]
        embed.add_field(name="🔧 Components", value="\n".join(lines), inline=False)

    if incidents:
        inc_lines = [f"🚨 **[{inc.get('impact','?').upper()}]** {inc['name']}" for inc in incidents[:3]]
        embed.add_field(name="⚠️ Active Incidents", value="\n".join(inc_lines), inline=False)
    else:
        embed.add_field(name="⚠️ Incidents", value="✅ No active incidents", inline=False)

    if maints:
        m_lines = [f"🔵 **{m['name']}** — starts {m.get('scheduled_for','TBD')[:10]}" for m in maints[:2]]
        embed.add_field(name="🗓️ Scheduled Maintenance", value="\n".join(m_lines), inline=False)

    embed.set_footer(text="🔄 Updates every 60 seconds  •  bwr7s.statuspage.io")
    return embed

# ── Bot ────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

class WarningsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Slash commands synced.")

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="for rule breakers 👀"
        ))
        if not update_status_embed.is_running():
            update_status_embed.start()

bot = WarningsBot()

def is_admin(interaction: discord.Interaction):
    m = interaction.user
    return isinstance(m, discord.Member) and (
        m.guild_permissions.administrator or
        m.guild_permissions.manage_guild or
        m.guild_permissions.moderate_members
    )

async def dm_user(user, embed):
    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False

# ── Status loop ────────────────────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def update_status_embed():
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if not channel:
            print(f"[Status] Channel not found: {STATUS_CHANNEL_ID}")
            return

        resp = requests.get(STATUS_PAGE_URL, timeout=10)
        if resp.status_code != 200:
            return

        embed = build_status_embed(resp.json())
        msg_id = None

        if os.path.exists(STATUS_MSG_FILE):
            with open(STATUS_MSG_FILE) as f:
                try:
                    msg_id = int(f.read().strip())
                except Exception:
                    msg_id = None

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
                print(f"[Status] Updated at {datetime.datetime.utcnow().strftime('%H:%M:%S')} UTC")
                return
            except discord.NotFound:
                pass

        msg = await channel.send(embed=embed)
        with open(STATUS_MSG_FILE, "w") as f:
            f.write(str(msg.id))
        print(f"[Status] Posted new embed (ID: {msg.id})")

    except Exception as e:
        print(f"[Status] Error: {e}")

# ── /warn ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You need **Moderate Members** or higher.", ephemeral=True)
        return
    await interaction.response.defer()

    warning_id   = str(uuid.uuid4())[:8].upper()
    timestamp    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    append_row([str(user.id), str(user), str(interaction.user), reason, "Warning",
                timestamp[:10], "N/A", warning_id, timestamp, "TRUE", "FALSE", "", "Discord"])

    dm_embed = discord.Embed(title="⚠️ You Have Been Warned",
                             description=f"You received a warning in **{interaction.guild.name}**.",
                             color=discord.Color.orange())
    dm_embed.add_field(name="Reason",    value=reason,              inline=False)
    dm_embed.add_field(name="Warning ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Issued by", value=str(interaction.user), inline=True)
    dm_embed.add_field(name="Total Active Warnings", value=str(active_count), inline=True)
    dm_embed.set_footer(text="Please follow the server rules to avoid further action.")
    dm_embed.timestamp = datetime.datetime.utcnow()
    dm_sent = await dm_user(user, dm_embed)

    embed = discord.Embed(title="⚠️ Warning Issued", color=discord.Color.orange())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User",           value=user.mention,             inline=True)
    embed.add_field(name="Warning ID",     value=f"`{warning_id}`",        inline=True)
    embed.add_field(name="Total Warnings", value=str(active_count),        inline=True)
    embed.add_field(name="Reason",         value=reason,                   inline=False)
    embed.add_field(name="Issued by",      value=interaction.user.mention, inline=True)
    embed.add_field(name="DM Sent",        value="✅ Yes" if dm_sent else "❌ DMs closed", inline=True)
    embed.set_footer(text=f"Use /revokewarning {warning_id} to undo")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

# ── /issuewarning ──────────────────────────────────────────────────────────────

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer()

    warning_id   = str(uuid.uuid4())[:8].upper()
    timestamp    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    append_row([str(user.id), str(user), str(interaction.user), reason, "Warning",
                timestamp[:10], "N/A", warning_id, timestamp, "TRUE", "FALSE", "", "Discord"])

    dm_embed = discord.Embed(title="⚠️ You Have Been Warned",
                             description=f"You received a warning in **{interaction.guild.name}**.",
                             color=discord.Color.orange())
    dm_embed.add_field(name="Reason",    value=reason,              inline=False)
    dm_embed.add_field(name="Warning ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Issued by", value=str(interaction.user), inline=True)
    dm_embed.set_footer(text="Please follow the server rules.")
    dm_embed.timestamp = datetime.datetime.utcnow()
    dm_sent = await dm_user(user, dm_embed)

    embed = discord.Embed(title="⚠️ Warning Issued", color=discord.Color.orange())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User",       value=user.mention,             inline=True)
    embed.add_field(name="Warning ID", value=f"`{warning_id}`",        inline=True)
    embed.add_field(name="Warnings",   value=str(active_count),        inline=True)
    embed.add_field(name="Reason",     value=reason,                   inline=False)
    embed.add_field(name="Issued by",  value=interaction.user.mention, inline=True)
    embed.add_field(name="DM Sent",    value="✅ Yes" if dm_sent else "❌ DMs closed", inline=True)
    embed.set_footer(text=f"Use /revokewarning {warning_id} to undo")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

# ── /revokewarning ─────────────────────────────────────────────────────────────

@bot.tree.command(name="revokewarning", description="[Admin] Revoke a warning by its ID")
@app_commands.describe(warning_id="Warning ID to revoke (e.g. AB12CD34)")
async def revokewarning(interaction: discord.Interaction, warning_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer()

    row, sheet_row = find_warning_by_id(warning_id)
    if row is None:
        await interaction.followup.send(f"❌ No warning found with ID `{warning_id}`.")
        return
    if row[COL_REVOKED].strip().upper() == "TRUE":
        await interaction.followup.send(f"⚠️ Warning `{warning_id}` is already revoked.")
        return

    row[COL_ACTIVE]     = "FALSE"
    row[COL_REVOKED]    = "TRUE"
    row[COL_REVOKED_BY] = str(interaction.user)
    update_row(sheet_row, row)

    try:
        warned_user = await bot.fetch_user(int(row[COL_USER_ID]))
        dm_embed = discord.Embed(title="✅ Warning Revoked",
                                 description=f"A warning in **{interaction.guild.name}** has been removed.",
                                 color=discord.Color.green())
        dm_embed.add_field(name="Warning ID",      value=f"`{warning_id}`",     inline=True)
        dm_embed.add_field(name="Original reason", value=row[COL_REASON],       inline=False)
        dm_embed.add_field(name="Revoked by",      value=str(interaction.user), inline=True)
        dm_embed.timestamp = datetime.datetime.utcnow()
        await warned_user.send(embed=dm_embed)
    except Exception:
        pass

    embed = discord.Embed(title="✅ Warning Revoked", color=discord.Color.green())
    embed.add_field(name="Warning ID",      value=f"`{warning_id}`",          inline=True)
    embed.add_field(name="User",            value=f"<@{row[COL_USER_ID]}>",   inline=True)
    embed.add_field(name="Original reason", value=row[COL_REASON],            inline=False)
    embed.add_field(name="Revoked by",      value=interaction.user.mention,   inline=True)
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

# ── /viewmywarnings ────────────────────────────────────────────────────────────

@bot.tree.command(name="viewmywarnings", description="View all your warnings (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))

    if not warnings:
        await interaction.followup.send("✅ You have no warnings on record!", ephemeral=True)
        return

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]

    embed = discord.Embed(title="📋 Your Warnings",
                          color=discord.Color.red() if active else discord.Color.green())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    if active:
        lines = [f"**`{r[COL_INCIDENT_ID]}`** — {r[COL_REASON]} *(by {r[COL_ISSUED_BY]}, {r[COL_TIMESTAMP]})*" for r, _ in active]
        embed.add_field(name=f"⚠️ Active ({len(active)})", value="\n".join(lines)[:1024], inline=False)
    if revoked:
        lines = [f"~~`{r[COL_INCIDENT_ID]}`~~ {r[COL_REASON]}" for r, _ in revoked]
        embed.add_field(name=f"✅ Revoked ({len(revoked)})", value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text=f"Total: {len(warnings)} | Active: {len(active)} | Revoked: {len(revoked)}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── /viewwarnings ──────────────────────────────────────────────────────────────

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user")
@app_commands.describe(user="The user to look up")
async def viewwarnings(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(user.id))

    if not warnings:
        await interaction.followup.send(f"✅ {user.mention} has no warnings.", ephemeral=True)
        return

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]

    embed = discord.Embed(title=f"📋 Warnings for {user.display_name}",
                          color=discord.Color.red() if active else discord.Color.green())
    embed.set_thumbnail(url=user.display_avatar.url)
    if active:
        lines = [f"**`{r[COL_INCIDENT_ID]}`** — {r[COL_REASON]} *(by {r[COL_ISSUED_BY]}, {r[COL_TIMESTAMP]})*" for r, _ in active]
        embed.add_field(name=f"⚠️ Active ({len(active)})", value="\n".join(lines)[:1024], inline=False)
    if revoked:
        lines = [f"~~`{r[COL_INCIDENT_ID]}`~~ {r[COL_REASON]}" for r, _ in revoked]
        embed.add_field(name=f"✅ Revoked ({len(revoked)})", value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text=f"Total: {len(warnings)} | Active: {len(active)} | Revoked: {len(revoked)}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Run ────────────────────────────────────────────────────────────────────────
# ── Run Web Server Framework & Bot ─────────────────────────────────────────────
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "BWR7 Warnings Bot is Online!", 200

def run_discord_bot():
    if not DISCORD_TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN is not set")
        return
    print("🤖 Starting Discord Bot inside background thread...")
    bot.run(DISCORD_TOKEN)

# This triggers the moment Gunicorn loads the 'app' object
print("🛰️ Initializing background threads...")
discord_thread = threading.Thread(target=run_discord_bot, daemon=True)
discord_thread.start()
