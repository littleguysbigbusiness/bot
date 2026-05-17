import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import uuid
import datetime
import requests
import threading
from flask import Flask
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1420690312531017850
STATIC_STATUS_ID  = 1505559844449419284  # Static message ID to edit in place

# API endpoints scaled to 15 columns (A:O)
SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

# Exact column indices matching your spreadsheet (A to O order)
COL_USER_ID     = 0   # user_id
COL_USERNAME    = 1   # username
COL_ISSUED_BY   = 2   # warned_by
COL_ISSUED_ID   = 3   # warned_by_id
COL_REASON      = 4   # reason
COL_TIMESTAMP   = 5   # timestamp
COL_INCIDENT_ID = 6   # warning_id
COL_REVOKED     = 7   # revoked (TRUE/FALSE)
COL_REVOKED_BY  = 8   # revoked_by
COL_REVOKED_AT  = 9   # revoked_at
COL_SOURCE      = 10  # source
COL_RESTRICTION = 11  # restriction
COL_START_DATE  = 12  # start_date
COL_END_DATE    = 13  # end_date
COL_ALT_INC_ID  = 14  # incident_id (Column O fallback)

# ── Google Service Account Authentication ─────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = None

if os.path.exists("service_account.json"):
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json", scopes=SCOPES
    )
else:
    print("⚠️ WARNING: service_account.json not found inside Render storage!")

def sheets_headers():
    global creds
    if creds:
        if not creds.valid:
            creds.refresh(Request())
        token = creds.token
    else:
        token = ""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── Sheets helpers ─────────────────────────────────────────────────────────────
def pad(row, length=15):
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
        resp = requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
        if resp.status_code != 200:
            print(f"[Sheets] Append failed with status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Sheets] Append error: {e}")

def update_row(row_index, row):
    try:
        range_str = f"{SHEET_NAME}!A{row_index}:O{row_index}"
        url = f"{SHEET_UPDATE_BASE}{range_str}?valueInputOption=RAW"
        resp = requests.put(url, headers=sheets_headers(), json={"values": [row]}, timeout=10)
        if resp.status_code != 200:
            print(f"[Sheets] Update failed with status {resp.status_code}: {resp.text}")
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
        if row[COL_USER_ID].strip() == str(user_id).strip():
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

# ── Bot Framework ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class WarningsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Slash commands synced.")

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="for rule breakers 👀")
        )
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

# ── Status Loop (Edits Your Static Message) ───────────────────────────────────
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

        try:
            msg = await channel.fetch_message(STATIC_STATUS_ID)
            await msg.edit(embed=embed)
            print(f"[Status] Successfully edited static message {STATIC_STATUS_ID}")
            return
        except discord.NotFound:
            print(f"⚠️ Could not find static message ID {STATIC_STATUS_ID} in channel.")
        except discord.Forbidden:
            print("❌ Lacking permissions to modify static message.")

    except Exception as e:
        print(f"[Status] Error: {e}")

# ── Commands ───────────────────────────────────────────────────────────────────
@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(
    user="The user to warn", 
    reason="Reason for the warning",
    source="Where did the infraction happen?",
    end_date="Optional expiry date (e.g., 2026-12-31). Leave blank for permanent."
)
@app_commands.choices(source=[
    app_commands.Choice(name="Discord Server", value="Discord"),
    app_commands.Choice(name="Roblox In-Game", value="Roblox Game")
])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You need **Moderate Members** or higher.", ephemeral=True)
        return
    await interaction.response.defer()

    warning_id   = str(uuid.uuid4())[:8].upper()
    timestamp    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date   = timestamp[:10]
    final_expiry = end_date if end_date else "Never"
    final_source = source.value if source else "Discord"

    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    # Appends exactly into your 15-column schema (A to O layout)
    append_row([
        str(user.id),          # A: user_id
        str(user),             # B: username
        str(interaction.user), # C: warned_by
        str(interaction.user.id), # D: warned_by_id
        reason,                # E: reason
        timestamp,             # F: timestamp
        warning_id,            # G: warning_id
        "FALSE",               # H: revoked (Starts as false)
        "",                    # I: revoked_by
        "",                    # J: revoked_at
        final_source,          # K: source
        "Warning",             # L: restriction (Forced to Warning)
        start_date,            # M: start_date
        final_expiry,          # N: end_date
        warning_id             # O: incident_id fallback
    ])

    # Clean UI DM panel
    dm_embed = discord.Embed(
        title="⚠️ User Moderation Notice",
        description="An official warning has been registered for your account profile. Please review our community standards to maintain server compliance.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    dm_embed.add_field(name="📋 Reason", value=f"```text\n{reason}\n```", inline=False)
    dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Issuer", value=str(interaction.user), inline=True)
    dm_embed.add_field(name="Platform", value=final_source, inline=True)
    dm_embed.add_field(name="Expires", value=final_expiry, inline=True)
    dm_embed.add_field(name="Total Active", value=f"**{active_count}**", inline=True)
    dm_embed.set_footer(text="Automated Compliance Engine")
    dm_embed.timestamp = datetime.datetime.utcnow()
    dm_sent = await dm_user(user, dm_embed)

    # Wide Channel Log Embed Panel
    embed = discord.Embed(
        title="🛑 User Log Added",
        description="A formal infraction record has been generated and securely synchronized with the central administration database.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    
    embed.add_field(name="User", value=f"{user.mention}\n`ID: {user.id}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Status", value="🟢 **Active**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="Source", value=final_source, inline=True)
    embed.add_field(name="Expiry Date", value=final_expiry, inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Dispatched" if dm_sent else "❌ Closed DMs", inline=True)
    
    embed.set_footer(text=f"To remove this case file, use: /revokewarning {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(
    user="The user to warn", 
    reason="Reason for the warning",
    source="Where did the infraction happen?",
    end_date="Optional expiry date (e.g., 2026-12-31). Leave blank for permanent."
)
@app_commands.choices(source=[
    app_commands.Choice(name="Discord Server", value="Discord"),
    app_commands.Choice(name="Roblox In-Game", value="Roblox Game")
])
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer()

    warning_id   = str(uuid.uuid4())[:8].upper()
    timestamp    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date   = timestamp[:10]
    final_expiry = end_date if end_date else "Never"
    final_source = source.value if source else "Discord"

    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    append_row([
        str(user.id), str(user), str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", final_source, "Warning", start_date, final_expiry, warning_id
    ])

    dm_embed = discord.Embed(
        title="⚠️ User Moderation Notice",
        description="An official warning has been registered for your account profile. Please review our community standards.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    dm_embed.add_field(name="📋 Reason", value=f"```text\n{reason}\n```", inline=False)
    dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Platform", value=final_source, inline=True)
    dm_embed.add_field(name="Expires", value=final_expiry, inline=True)
    dm_embed.set_footer(text="Automated Compliance Engine")
    dm_embed.timestamp = datetime.datetime.utcnow()
    dm_sent = await dm_user(user, dm_embed)

    embed = discord.Embed(
        title="🛑 User Log Added",
        description="A formal infraction record has been generated and securely synchronized with the central database.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    
    embed.add_field(name="User", value=f"{user.mention}\n`ID: {user.id}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Status", value="🟢 **Active**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="Source", value=final_source, inline=True)
    embed.add_field(name="Expiry Date", value=final_expiry, inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Dispatched" if dm_sent else "❌ Closed DMs", inline=True)
    
    embed.set_footer(text=f"To remove this case file, use: /revokewarning {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="revokewarning", description="[Admin] Revoke a warning by its ID")
@app_commands.describe(warning_id="Warning ID to revoke (e.g. AB12CD34)")
async def revokewarning(interaction: discord.Interaction, warning_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer()

    row, sheet_row = find_warning_by_id(warning_id)
    if row is None:
        await interaction.followup.send(f"❌ No case file found with ID `{warning_id}`.")
        return
    if row[COL_REVOKED].strip().upper() == "TRUE":
        await interaction.followup.send(f"⚠️ Case file `{warning_id}` has already been removed.")
        return

    # Updates your tracking status flags cleanly
    row[COL_REVOKED]    = "TRUE"
    row[COL_REVOKED_BY] = str(interaction.user)
    row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    update_row(sheet_row, row)

    try:
        warned_user = await bot.fetch_user(int(row[COL_USER_ID]))
        dm_embed = discord.Embed(title="✅ Record Updated",
                                 description=f"A prior infraction file in **{interaction.guild.name}** has been removed.",
                                 color=discord.Color.from_rgb(39, 174, 96))
        dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
        dm_embed.add_field(name="Original Reason", value=row[COL_REASON], inline=False)
        dm_embed.timestamp = datetime.datetime.utcnow()
        await warned_user.send(embed=dm_embed)
    except Exception:
        pass

    embed = discord.Embed(title="✅ User Log Updated", color=discord.Color.from_rgb(39, 174, 96))
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="User File", value=f"<@{row[COL_USER_ID]}>", inline=True)
    embed.add_field(name="Status Update", value="⚪ **Removed / Revoked**", inline=True)
    embed.add_field(name="Original Reason", value=row[COL_REASON], inline=False)
    embed.add_field(name="Revoked By", value=interaction.user.mention, inline=True)
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="viewmywarnings", description="View all your warnings (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))

    if not warnings:
        embed = discord.Embed(
            title="User Moderation History",
            description="This menu is where you can view your past and current moderation history. Administrators can add incidents for any type of rule breaking.",
            color=discord.Color.from_rgb(44, 62, 80)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]

    embed = discord.Embed(
        title="User Moderation History",
        description="This menu is where you can view your past and current moderation history. Administrators can add incidents for any type of rule breaking.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    if active:
        block_text = ""
        for r, _ in active:
            block_text += f"▪️ ID: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Source: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        embed.add_field(name=f"🟢 Active Records ({len(active)})", value=f"```text\n{block_text.strip()}\n```", inline=False)
        
    if revoked:
        block_text = ""
        for r, _ in revoked:
            block_text += f"▪️ ID: {r[COL_INCIDENT_ID]} | {r[COL_REASON]} (Revoked)\n"
        embed.add_field(name=f"⚪ Historical Archive ({len(revoked)})", value=f"```text\n{block_text.strip()}\n```", inline=False)

    embed.set_footer(text=f"Total Incidents Logged: {len(warnings)}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user")
@app_commands.describe(user="The user to look up")
async def viewwarnings(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(user.id))

    if not warnings:
        embed = discord.Embed(
            title=f"User Log File — {user.display_name}",
            description="This menu is where you can view all past and current moderation history. Moderators can add incidents for any type of rule breaking.",
            color=discord.Color.from_rgb(44, 62, 80)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]

    embed = discord.Embed(
        title=f"User Log File — {user.display_name}",
        description="This menu is where you can view all past and current moderation history. Moderators can add incidents for any type of rule breaking.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    if active:
        block_text = ""
        for r, _ in active:
            block_text += f"▪️ Case: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Source: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        embed.add_field(name=f"🟢 Active Records ({len(active)})", value=f"```text\n{block_text.strip()}\n```", inline=False)
        
    if revoked:
        block_text = ""
        for r, _ in revoked:
            block_text += f"▪️ Case: {r[COL_INCIDENT_ID]} | {r[COL_REASON]} (Revoked)\n"
        embed.add_field(name=f"⚪ Historical Archive ({len(revoked)})", value=f"```text\n{block_text.strip()}\n```", inline=False)

    embed.set_footer(text=f"Total Incidents Logged: {len(warnings)}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Production Flask Engine Server ─────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "BWR7 Warnings Bot is Online Framework Stable!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def run_discord_bot():
    if not DISCORD_TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN environment variable is empty.")
        return
    print("🤖 Starting Discord Bot inside background thread...")
    bot.run(DISCORD_TOKEN)

# Launch pipelines
print("🛰️ Initializing multi-threaded background pipelines...")
web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()

discord_thread = threading.Thread(target=run_discord_bot, daemon=True)
discord_thread.start()
