import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import uuid
import datetime
import requests
import threading
import json
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1476812926521184276  
STATIC_STATUS_ID  = 1505559844449419284  
APPEAL_CHANNEL_ID = 1420690312531017850  

# Your official application link assets
GOOGLE_APPEAL_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLScOXFE24Bz7jGiNT4kn02FLiADibivHIxREyXGY2rvQxACG-A/viewform?usp=dialog" 

# Scale endpoints to 16 columns (A:P) to include the role database column
SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:P"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:P:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

# Column indices matching your spreadsheet (A to P layout)
COL_USER_ID     = 0
COL_USERNAME    = 1
COL_ISSUED_BY   = 2
COL_ISSUED_ID   = 3
COL_REASON      = 4
COL_TIMESTAMP   = 5
COL_INCIDENT_ID = 6
COL_REVOKED     = 7
COL_REVOKED_BY  = 8
COL_REVOKED_AT  = 9
COL_SOURCE      = 10
COL_RESTRICTION = 11
COL_START_DATE  = 12
COL_END_DATE    = 13
COL_ALT_INC_ID  = 14
COL_BACKUP_ROLES = 15  # Column P: Stores comma-separated staff role IDs

# Explicitly protected role names: IMMUNE to being stripped and BLOCKED from auto-restoration
PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

# ── Google Service Account Authentication ─────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = None

# 🛰️ Dynamic routing check updates to search Render's system secret directory mount path first!
SECRET_FILE_PATH = "/etc/secrets/service_account.json"
ALTERNATIVE_PATH = "service_account.json"

TARGET_PATH = SECRET_FILE_PATH if os.path.exists(SECRET_FILE_PATH) else ALTERNATIVE_PATH

if os.path.exists(TARGET_PATH):
    try:
        creds = service_account.Credentials.from_service_account_file(
            TARGET_PATH, scopes=SCOPES
        )
        print(f"✅ Google API credentials initialized cleanly from: {TARGET_PATH}")
    except Exception as e:
        print(f"❌ Google API initialization exception: {e}")
else:
    print("⚠️ WARNING: 'service_account.json' could not be found anywhere inside system paths!")

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
def pad(row, length=16):
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
        range_str = f"{SHEET_NAME}!A{row_index}:P{row_index}"
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
        if row[COL_USER_ID].strip() == str(user_id).strip():
            results.append((row, i + 2))
    return results

# ── Status Page ────────────────────────────────────────────────────────────────
STATUS_EMOJI = {"operational": "✅", "degraded_performance": "🟨", "partial_outage": "🟧", "major_outage": "🔴", "under_maintenance": "🔵", "unknown": "⬜"}
STATUS_COLOR = {"none": discord.Color.green(), "minor": discord.Color.yellow(), "major": discord.Color.red(), "critical": discord.Color.red(), "maintenance": discord.Color.blue(), "unknown": discord.Color.light_grey()}

def status_label(s):
    return s.replace("_", " ").title()

def build_status_embed(data):
    page, status, components = data.get("page", {}), data.get("status", {}), data.get("components", [])
    incidents, maints = data.get("incidents", []), data.get("scheduled_maintenances", [])
    indicator = status.get("indicator", "unknown")
    embed = discord.Embed(title=f"📡 {page.get('name', 'Status Page')} — Live Status", url=page.get("url", "https://bwr7s.statuspage.io"), description=f"**Overall:** {STATUS_EMOJI.get(indicator, '⬜')} {status.get('description', 'Unknown')}", color=STATUS_COLOR.get(indicator, discord.Color.light_grey()), timestamp=datetime.datetime.utcnow())
    visible = [c for c in components if not c.get("group", False)]
    if visible:
        lines = [f"{STATUS_EMOJI.get(c.get('status','unknown'), '⬜')} **{c['name']}** — {status_label(c.get('status','unknown'))}" for c in visible]
        embed.add_field(name="🔧 Components", value="\n".join(lines), inline=False)
    if incidents:
        embed.add_field(name="⚠️ Active Incidents", value="\n".join([f"🚨 **[{inc.get('impact','?').upper()}]** {inc['name']}" for inc in incidents[:3]]), inline=False)
    else:
        embed.add_field(name="⚠️ Incidents", value="✅ No active incidents", inline=False)
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
        self.add_view(AppealReviewButtons()) 
        await self.tree.sync()
        print("✅ Slash commands synced.")
    async def on_ready(self):
        print(f"✅ Logged in as {self.user}")
        if not update_status_embed.is_running():
            update_status_embed.start()

bot = WarningsBot()

def is_admin(interaction: discord.Interaction):
    m = interaction.user
    return isinstance(m, discord.Member) and (m.guild_permissions.administrator or m.guild_permissions.manage_guild or m.guild_permissions.moderate_members)

async def dm_user(user, embed):
    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False

@tasks.loop(seconds=60)
async def update_status_embed():
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if not channel: return
        resp = requests.get(STATUS_PAGE_URL, timeout=10)
        if resp.status_code == 200:
            embed = build_status_embed(resp.json())
            try:
                msg = await channel.fetch_message(STATIC_STATUS_ID)
                await msg.edit(embed=embed)
            except Exception: pass
    except Exception: pass

# ── Interactive Appeal System Elements ───────────────────────────────────────
class AppealReasonModal(discord.ui.Modal, title="Submit Case File Appeal"):
    appeal_reason = discord.ui.TextInput(label="Why should this infraction be removed?", style=discord.TextStyle.paragraph, placeholder="Provide clean context or evidence for review...", required=True, max_length=500)

    def __init__(self, case_id, original_reason, restriction_type):
        super().__init__()
        self.case_id = case_id
        self.original_reason = original_reason
        self.restriction_type = restriction_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        review_channel = bot.get_channel(APPEAL_CHANNEL_ID)
        if not review_channel:
            await interaction.followup.send("❌ Internal Error: Appeal review channel missing.", ephemeral=True)
            return

        review_embed = discord.Embed(title="📥 System Infraction Appeal Submitted", description=f"User {interaction.user.mention} has requested a file evaluation regarding an active system restriction.", color=discord.Color.from_rgb(230, 126, 34))
        review_embed.add_field(name="👤 Appellant", value=f"{interaction.user.mention}\n`ID: {interaction.user.id}`", inline=True)
        review_embed.add_field(name="🆔 Target Case ID", value=f"`{self.case_id}`", inline=True)
        review_embed.add_field(name="📊 Action Type", value=self.restriction_type, inline=True)
        review_embed.add_field(name="📋 Original Reason Cell", value=f"```text\n{self.original_reason}\n```", inline=False)
        review_embed.add_field(name="💬 Appellant Statement", value=f"```text\n{self.appeal_reason.value}\n```", inline=False)
        review_embed.timestamp = datetime.datetime.utcnow()

        await review_channel.send(embed=review_embed, view=AppealReviewButtons())
        await interaction.followup.send("✅ **Success!** Your appeal has been sent to administration for review.", ephemeral=True)

class AppealDropdownMenu(discord.ui.Select):
    def __init__(self, user_active_cases):
        options = []
        for row, _ in user_active_cases:
            case_id = row[COL_INCIDENT_ID].strip()
            rest_type = row[COL_RESTRICTION].strip()
            reason = row[COL_REASON].strip()
            options.append(discord.SelectOption(label=f"Case: {case_id} [{rest_type}]", value=case_id, description=reason[:50]))
        super().__init__(placeholder="Select an active infraction file to appeal...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        case_id = self.values[0]
        row, _ = find_warning_by_id(case_id)
        if not row: return await interaction.response.send_message("❌ Case resolved or moved.", ephemeral=True)
        await interaction.response.send_modal(AppealReasonModal(case_id, row[COL_REASON], row[COL_RESTRICTION]))

class AppealDropdownView(discord.ui.View):
    def __init__(self, user_active_cases):
        super().__init__(timeout=180)
        self.add_item(AppealDropdownMenu(user_active_cases))

class AppealReviewButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve Appeal", style=discord.ButtonStyle.success, custom_id="approve_appeal_btn", emoji="🟢")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer()
        embed = interaction.message.embeds[0]
        case_id = embed.fields[1].value.replace("`", "").strip()
        appellant_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        row, sheet_row = find_warning_by_id(case_id)
        if row and row[COL_REVOKED].strip().upper() != "TRUE":
            await execute_live_punishment_revocation(interaction.guild, row, str(interaction.user))
            row[COL_REVOKED]    = "TRUE"
            row[COL_REVOKED_BY] = f"Appeal Appr: {interaction.user}"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(sheet_row, row)

        try:
            target_user = await bot.fetch_user(appellant_id)
            await target_user.send(embed=discord.Embed(title="✅ Appeal Approved", description=f"Your appeal for Case ID `{case_id}` has been accepted. Your restrictions and original staff roles have been restored.", color=discord.Color.green()))
        except Exception: pass

        embed.color, embed.title = discord.Color.green(), "✅ Appeal Cleared & Approved"
        embed.set_footer(text=f"Approved and lifted by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(label="Deny Appeal", style=discord.ButtonStyle.danger, custom_id="deny_appeal_btn", emoji="🔴")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer()
        embed = interaction.message.embeds[0]
        case_id = embed.fields[1].value.replace("`", "").strip()
        appellant_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        try:
            target_user = await bot.fetch_user(appellant_id)
            await target_user.send(embed=discord.Embed(title="❌ Appeal Denied", description=f"Your appeal for Case ID `{case_id}` has been rejected.", color=discord.Color.red()))
        except Exception: pass

        embed.color, embed.title = discord.Color.red(), "❌ Appeal Evaluated & Rejected"
        embed.set_footer(text=f"Rejected upon review by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

# ── Automated Active Penalty Lift Engine ───────────────────────────────────────
async def execute_live_punishment_revocation(guild: discord.Guild, row, admin_name: str) -> str:
    uid = int(row[COL_USER_ID].strip())
    rest_type = row[COL_RESTRICTION].strip()
    source_context = row[COL_SOURCE].strip()

    if source_context != "Discord": return "Logged to Sheet (In-Game Context)"

    if rest_type == "Timeout":
        try:
            member = await guild.fetch_member(uid)
            if member:
                await member.timeout(None, reason=f"Universal Revoke executed by {admin_name}")
                return "Timeout lifted cleanly (User Unmuted)"
        except Exception as e: return f"Timeout lift error: {e}"

    elif rest_type == "Ban":
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Universal Revoke executed by {admin_name}")
            return "Ban successfully lifted (User Unbanned)"
        except Exception as e: return f"API Unban execution failed: {e}"

    elif rest_type == "Staff Suspension":
        raw_roles = row[COL_BACKUP_ROLES].strip()
        if not raw_roles: return "Staff Suspension lifted (No spreadsheet backup roles stored)"
        
        try:
            member = await guild.fetch_member(uid)
            if member:
                restored = 0
                role_ids = [int(r.strip()) for r in raw_roles.split(",") if r.strip().isdigit()]
                for r_id in role_ids:
                    role = guild.get_role(r_id)
                    if role:
                        if role.name.strip() in PROTECTED_ROLE_NAMES:
                            continue
                        try: 
                            await member.add_roles(role)
                            restored += 1
                        except Exception: pass
                return f"Staff Suspension lifted ({restored} spreadsheet roles restored)"
        except Exception as e: return f"Staff Suspension role allocation error: {e}"

    return "Database trail flagged"

# ── Universal Action Master Engine ─────────────────────────────────────────────
async def run_moderation_action(interaction: discord.Interaction, user: discord.Member, reason: str, restriction_type: str, source: str, final_expiry: str, duration_delta: datetime.timedelta = None, backup_roles_str: str = "") -> str:
    warning_id = str(uuid.uuid4())[:8].upper()
    timestamp  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date = timestamp[:10]

    roblox_username = user.nick if user.nick else user.display_name

    dm_embed = discord.Embed(title=f"⚠️ Account Moderation Notice: {restriction_type.upper()}", description=f"A formal system action has been registered against your account profile inside **{interaction.guild.name}** due to a rules violation.", color=discord.Color.from_rgb(44, 62, 80))
    dm_embed.add_field(name="📋 Infraction Type", value=restriction_type, inline=True)
    dm_embed.add_field(name="📋 Stated Reason", value=f"```text\n{reason}\n```", inline=False)
    dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Platform context", value=source, inline=True)
    dm_embed.add_field(name="Expiration Date", value=final_expiry, inline=True)
    
    form_instructions = f"If you are banned or restricted from server channels and cannot submit an in-app `/appeal`, you may lodge an external case evaluation request via our primary appeal form:\n📋 **[Click Here to Open Google Appeal Form]({GOOGLE_APPEAL_FORM_URL})**"
    dm_embed.add_field(name="⚖️ External Appeal Request Notice", value=form_instructions, inline=False)
    dm_embed.set_footer(text="Automated Compliance Engine • Busways Administration")
    dm_embed.timestamp = datetime.datetime.utcnow()

    dm_sent = await dm_user(user, dm_embed)

    execution_notes = "Logged to Database"
    if source == "Discord":
        if restriction_type == "Timeout" and duration_delta:
            try:
                await user.timeout(duration_delta, reason=reason)
                execution_notes = f"Timed out natively until UTC timestamp"
            except Exception as e: execution_notes = f"Logged (Timeout API call failed: {e})"
        elif restriction_type == "Ban":
            try:
                await user.ban(delete_message_days=1, reason=reason)
                execution_notes = "Banned cleanly from guild instance"
            except Exception as e: execution_notes = f"Logged (API Ban execution failed: {e})"

    append_row([
        str(user.id), roblox_username, str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", source, restriction_type, start_date, final_expiry, warning_id,
        backup_roles_str  
    ])

    embed = discord.Embed(title=f"🛑 User Log Added ({restriction_type})", description=f"A formal {restriction_type.lower()} record has been generated and securely logged to the central database.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User Profile", value=f"{user.mention}\n`Roblox: {roblox_username}`\n`ID: {user.id}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Type", value=f"**{restriction_type}**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="API Execution", value=f"`{execution_notes}`", inline=True)
    embed.add_field(name="Expiry Timestamp", value=f"`{final_expiry}`", inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Pre-Dispatched" if dm_sent else "❌ Closed DMs", inline=True)
    embed.set_footer(text=f"To undo this record file, execute: /revokeaction {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

# ── General Lookups & Revocation Dashboard Commands ───────────────────────────
@bot.tree.command(name="revokeaction", description="[Admin] Revoke an active moderation file and instantly lift its punishment")
@app_commands.describe(case_id="The Case ID to revoke (e.g. AB12CD34)")
async def revokeaction(interaction: discord.Interaction, case_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    row, sheet_row = find_warning_by_id(case_id)
    if row is None: return await interaction.followup.send(f"❌ Case ID `{case_id}` not found on database sheet.")
    if row[COL_REVOKED].strip().upper() == "TRUE": return await interaction.followup.send(f"⚠️ Case file `{case_id}` has already been revoked.")

    lift_result = await execute_live_punishment_revocation(interaction.guild, row, str(interaction.user))

    row[COL_REVOKED]    = "TRUE"
    row[COL_REVOKED_BY] = str(interaction.user)
    row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    update_row(sheet_row, row)

    embed = discord.Embed(title="✅ Universal Revoke Executed", color=discord.Color.from_rgb(39, 174, 96))
    embed.add_field(name="Case ID", value=f"`{case_id}`", inline=True)
    embed.add_field(name="User Target", value=f"<@{row[COL_USER_ID]}>", inline=True)
    embed.add_field(name="Action Type Lifted", value=f"**{row[COL_RESTRICTION]}**", inline=True)
    embed.add_field(name="API Removal Result", value=f"`{lift_result}`", inline=False)
    embed.add_field(name="Original Reason Cell", value=row[COL_REASON], inline=False)
    embed.add_field(name="Authorized Administrator", value=interaction.user.mention, inline=True)
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="modstats", description="[Admin] View server-wide moderation metrics and leaderboard layout")
async def modstats(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    rows = read_all_rows()
    if not rows: return await interaction.followup.send("📋 Database empty.")

    total_logs = len(rows)
    type_counts = {"Warning": 0, "Timeout": 0, "Ban": 0, "Staff Suspension": 0}
    user_infractions, admin_actions = {}, {}

    for raw_row in rows:
        row = pad(raw_row)
        is_revoked = row[COL_REVOKED].strip().upper() == "TRUE"
        rest_type = row[COL_RESTRICTION].strip()
        uid = row[COL_USER_ID].strip()
        username = row[COL_USERNAME].strip()
        admin_name = row[COL_ISSUED_BY].strip()

        if rest_type in type_counts: type_counts[rest_type] += 1
        if not is_revoked and uid:
            user_key = f"<@{uid}> (`{username}`)"
            user_infractions[user_key] = user_infractions.get(user_key, 0) + 1
        if admin_name: admin_actions[admin_name] = admin_actions.get(admin_name, 0) + 1

    top_user = "None"
    if user_infractions:
        tk = max(user_infractions, key=user_infractions.get)
        top_user = f"{tk} — **{user_infractions[tk]}** active cases"

    top_admin = "None"
    if admin_actions:
        ak = max(admin_actions, key=admin_actions.get)
        top_admin = f"👤 **{ak}** — **{admin_actions[ak]}** actions logged"

    embed = discord.Embed(title="📊 Server Moderation Analytics Overview", description="Data compiled dynamically from tracking sheet.", color=discord.Color.from_rgb(44, 62, 80))
    embed.add_field(name="📈 Metrics Scale", value=f"Total Records Logged: **{total_logs}**", inline=False)
    embed.add_field(name="🗂️ Action Type Breakdown", value=f"⚠️ **Warnings:** {type_counts['Warning']}\n⏳ **Timeouts:** {type_counts['Timeout']}\n🔨 **Bans:** {type_counts['Ban']}\n🛡️ **Staff Suspensions:** {type_counts['Staff Suspension']}", inline=True)
    embed.add_field(name="🚨 Highest Active Infractions User", value=top_user, inline=False)
    embed.add_field(name="👮 Top Enforcing Administrator", value=top_admin, inline=False)
    embed.timestamp = datetime.datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="appeal", description="Submit an evaluation appeal form for an active infraction file")
async def appeal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    active_cases = [(r, idx) for r, idx in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not active_cases: return await interaction.followup.send("✅ You have no active warnings or restrictions available to appeal!", ephemeral=True)
    await interaction.followup.send("📋 **Infraction System Appeal Port:**\nSelect the case file from the dropdown:", view=AppealDropdownView(active_cases), ephemeral=True)

# ── Split Source Lookup Functions ─────────────────────────────────────────────
def compile_split_warnings_embed(embed, warnings):
    active_discord = []
    active_roblox = []
    revoked_list = []

    for r, _ in warnings:
        padded = pad(r)
        is_revoked = padded[COL_REVOKED].strip().upper() == "TRUE"
        src = padded[COL_SOURCE].strip().lower()
        
        case_text = f"▪️ Case: {padded[COL_INCIDENT_ID]} | Type: {padded[COL_RESTRICTION]}\n  Reason: {padded[COL_REASON]}\n  Issued: {padded[COL_TIMESTAMP][:10]} | Expires: {padded[COL_END_DATE]}\n\n"
        
        if is_revoked:
            revoked_list.append(f"▪️ Case: {padded[COL_INCIDENT_ID]} | Type: {padded[COL_RESTRICTION]} (Revoked)\n")
        elif "roblox" in src:
            active_roblox.append(case_text)
        else:
            active_discord.append(case_text)

    if active_discord:
        embed.add_field(name=f"🟢 Active Discord Records ({len(active_discord)})", value=f"```text\n{''.join(active_discord).strip()}\n```", inline=False)
    if active_roblox:
        embed.add_field(name=f"🎮 Active Roblox Records ({len(active_roblox)})", value=f"```text\n{''.join(active_roblox).strip()}\n```", inline=False)
    if revoked_list:
        embed.add_field(name=f"⚪ Historical Archive ({len(revoked_list)})", value=f"```text\n{''.join(revoked_list).strip()}\n```", inline=False)
        
    embed.set_footer(text=f"Total Infractions Cataloged: {len(warnings)}")
    return embed

@bot.tree.command(name="viewmywarnings", description="View all your warnings split between Discord and Roblox (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    
    embed = discord.Embed(title="User Moderation History", description="Your logs are categorized below based on where the infraction occurred.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    if not warnings:
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)
        
    embed = compile_split_warnings_embed(embed, warnings)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user split by platform")
@app_commands.describe(user="The user to look up")
async def viewwarnings(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(user.id))
    
    embed = discord.Embed(title=f"User Log File — {user.display_name}", description="Logs categorized below based on where the infraction occurred.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)
    
    if not warnings:
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)
        
    embed = compile_split_warnings_embed(embed, warnings)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Moderation Commands (Automated Discord Sourcing) ───────────────────────────
@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning", end_date="Optional expiry date (YYYY-MM-DD)")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    expiry = end_date if end_date else "Never"
    await run_moderation_action(interaction, user, reason, "Warning", "Discord", expiry)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning", end_date="Optional expiry date (YYYY-MM-DD)")
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    expiry = end_date if end_date else "Never"
    await run_moderation_action(interaction, user, reason, "Warning", "Discord", expiry)

@bot.tree.command(name="timeout", description="[Admin] Time out a user natively using duration variables")
@app_commands.describe(user="The user to mute", reason="Reason for timeout", duration_amount="The number value for time length", duration_unit="The unit of time measurement")
@app_commands.choices(duration_unit=[app_commands.Choice(name="Minutes", value="minutes"), app_commands.Choice(name="Hours", value="hours"), app_commands.Choice(name="Days", value="days")])
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, duration_amount: int, duration_unit: app_commands.Choice[str]):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    if duration_amount <= 0: return await interaction.followup.send("❌ **Error:** Duration must be a positive number.")

    unit = duration_unit.value
    if unit == "minutes": delta = datetime.timedelta(minutes=duration_amount)
    elif unit == "hours": delta = datetime.timedelta(hours=duration_amount)
    else: delta = datetime.timedelta(days=duration_amount)

    expiry_time = datetime.datetime.utcnow() + delta
    final_expiry_stamp = expiry_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, user, reason, "Timeout", "Discord", final_expiry_stamp, duration_delta=delta)

@bot.tree.command(name="ban", description="[Admin] Ban a user natively from the server and log to sheet")
@app_commands.describe(user="The user to ban", reason="Reason for ban", end_date="Optional expiry date (YYYY-MM-DD)")
async def ban_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    expiry = end_date if end_date else "Never"
    await run_moderation_action(interaction, user, reason, "Ban", "Discord", expiry)

@bot.tree.command(name="staff_suspension", description="[Admin] Suspend a staff member and save roles to Sheet")
@app_commands.describe(user="The staff member", reason="Reason for suspension", end_date="Expiry date (YYYY-MM-DD) REQUIRED")
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    try: datetime.datetime.strptime(end_date.strip(), "%Y-%m-%d")
    except ValueError: return await interaction.followup.send("❌ **Error:** Expiry date format required: `YYYY-MM-DD`")

    role_ids = [str(r.id) for r in user.roles if r.name != "@everyone" and not r.managed]
    backup_roles_str = ",".join(role_ids)

    for role in user.roles:
        if role.name != "@everyone" and not role.managed:
            if role.name.strip() in PROTECTED_ROLE_NAMES:
                continue
            try: await user.remove_roles(role)
            except Exception: pass
            
    await run_moderation_action(interaction, user, reason, "Staff Suspension", "Discord", end_date, backup_roles_str=backup_roles_str)

# ── PRODUCTION ROBLOX INBOUND ENDPOINTS ─────────────────────────────────────────
@app.route('/api/roblox/violation', methods=['POST'])
def roblox_violation_inbound():
    try:
        data = request.json
        action = data.get("action")
        
        if action == "add":
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            append_row([
                str(data.get("userId")), str(data.get("username")), str(data.get("issuedBy")), "",
                str(data.get("reason")), timestamp, str(data.get("incidentId")), "FALSE", "", "",
                "Roblox Game", str(data.get("restriction")), str(data.get("startDate")), str(data.get("endDate")), str(data.get("incidentId")), ""
            ])
            print(f"[API] Logged inbound Roblox Game violation file: {data.get('incidentId')}")
            return jsonify({"status": "success", "message": "Logged successfully"}), 200
            
        elif action == "revoke":
            case_id = str(data.get("incidentId")).strip().upper()
            row, sheet_row = find_warning_by_id(case_id)
            if row:
                row[COL_REVOKED]    = "TRUE"
                row[COL_REVOKED_BY] = f"In-Game: {data.get('revokedBy')}"
                row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                update_row(sheet_row, row)
                print(f"[API] Revoked inbound Roblox Game infraction file: {case_id}")
                return jsonify({"status": "success", "message": "Revoked successfully"}), 200
            return jsonify({"status": "error", "message": "Case ID not found"}), 404
            
    except Exception as e:
        print(f"[API Error] Inbound route breakdown: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ── Production Flask Engine Server ─────────────────────────────────────────────
app = Flask(__name__)
def run_web_server(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
def run_discord_bot():
    if not DISCORD_TOKEN: 
        print("❌ CRITICAL: 'DISCORD_BOT_TOKEN' environment variable is blank or missing inside Render settings!")
        return
    bot.run(DISCORD_TOKEN)

print("🛰️ Initializing multi-threaded background pipelines...")
threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=run_discord_bot, daemon=True).start()
