import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import uuid
import datetime
import requests
import threading
import json
from flask import Flask

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1476812926521184276  
STATIC_STATUS_ID  = 1505808587807789117  
APPEAL_CHANNEL_ID = 1505891264032149574  

# Your official application appeal assets
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

ROLES_BACKUP_FILE = "suspended_roles.json"

# Column indices
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

# Explicitly protected role names: IMMUNE to getting stripped!
PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

# ── Google Authentication (Render Secret Directory Path Alignment) ────────────
from google.oauth2 import service_account
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = None

SECRET_FILE_PATH = "/etc/secrets/service_account.json"
ALTERNATIVE_PATH = "service_account.json"
TARGET_PATH = SECRET_FILE_PATH if os.path.exists(SECRET_FILE_PATH) else ALTERNATIVE_PATH

if os.path.exists(TARGET_PATH):
    try:
        creds = service_account.Credentials.from_service_account_file(TARGET_PATH, scopes=SCOPES)
        print(f"✅ Google Sheets engine connected using file path: {TARGET_PATH}")
    except Exception as e:
        print(f"❌ Credentials parsing issue: {e}")
else:
    print("⚠️ Warning: service_account.json missing from environment entirely!")

def sheets_headers():
    global creds
    if creds:
        if not creds.valid:
            creds.refresh(Request())
        token = creds.token
    else:
        token = ""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── Sheets Helpers ─────────────────────────────────────────────────────────────
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
        requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
    except Exception as e:
        print(f"[Sheets] Append error: {e}")

def update_row(row_index, row):
    try:
        range_str = f"{SHEET_NAME}!A{row_index}:O{row_index}"
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

# ── Roles Backup Helpers ───────────────────────────────────────────────────────
def save_suspended_roles(user_id, role_ids):
    data = {}
    if os.path.exists(ROLES_BACKUP_FILE):
        try:
            with open(ROLES_BACKUP_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[str(user_id)] = role_ids
    with open(ROLES_BACKUP_FILE, "w") as f:
        json.dump(data, f)

def pop_suspended_roles(user_id):
    if not os.path.exists(ROLES_BACKUP_FILE):
        return []
    try:
        with open(ROLES_BACKUP_FILE, "r") as f:
            data = json.load(f)
        role_ids = data.pop(str(user_id), [])
        with open(ROLES_BACKUP_FILE, "w") as f:
            json.dump(data, f)
        return role_ids
    except Exception:
        return []

# ── Status Page Embedding Logic ───────────────────────────────────────────────
STATUS_EMOJI = {"operational": "✅", "degraded_performance": "🟨", "partial_outage": "🟧", "major_outage": "🔴", "under_maintenance": "🔵", "unknown": "⬜"}
STATUS_COLOR = {"none": discord.Color.green(), "minor": discord.Color.yellow(), "major": discord.Color.red(), "critical": discord.Color.red(), "maintenance": discord.Color.blue(), "unknown": discord.Color.light_grey()}

def status_label(s):
    return s.replace("_", " ").title()

def build_status_embed(data):
    page = data.get("page", {})
    status = data.get("status", {})
    components = data.get("components", [])
    incidents = data.get("incidents", [])
    indicator = status.get("indicator", "unknown")
    
    embed = discord.Embed(
        title=f"📡 {page.get('name', 'Status Page')} — Live Status",
        url=page.get("url", "https://bwr7s.statuspage.io"),
        description=f"**Overall:** {STATUS_EMOJI.get(indicator, '⬜')} {status.get('description', 'Unknown')}",
        color=STATUS_COLOR.get(indicator, discord.Color.light_grey()),
        timestamp=datetime.datetime.utcnow()
    )
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

# ── Bot Class ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class WarningsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self):
        self.add_view(AppealReviewButtons())
        # 🟢 BACK TO HOW IT WAS: Restored to standard global sync tracking framework
        await self.tree.sync()
        print("✅ Slash commands synced globally.")

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

# ── Interactive Appeal System Views ───────────────────────────────────────────
class AppealReasonModal(discord.ui.Modal, title="Submit Case File Appeal"):
    appeal_reason = discord.ui.TextInput(label="Why should this infraction be removed?", style=discord.TextStyle.paragraph, placeholder="Provide context or evidence...", required=True, max_length=500)

    def __init__(self, case_id, original_reason, restriction_type):
        super().__init__()
        self.case_id = case_id
        self.original_reason = original_reason
        self.restriction_type = restriction_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        review_channel = bot.get_channel(APPEAL_CHANNEL_ID)
        if not review_channel:
            await interaction.followup.send("❌ Error: Appeal review channel missing.", ephemeral=True)
            return

        review_embed = discord.Embed(title="📥 System Infraction Appeal Submitted", description="User has requested a file evaluation regarding an active system restriction.", color=discord.Color.from_rgb(230, 126, 34))
        review_embed.add_field(name="👤 Appellant", value=f"{interaction.user.mention}\n`ID: {interaction.user.id}`", inline=True)
        review_embed.add_field(name="🆔 Target Case ID", value=f"`{self.case_id}`", inline=True)
        review_embed.add_field(name="📊 Action Type", value=self.restriction_type, inline=True)
        review_embed.add_field(name="📋 Original Reason Cell", value=f"```text\n{self.original_reason}\n```", inline=False)
        review_embed.add_field(name="💬 Appellant Statement", value=f"```text\n{self.appeal_reason.value}\n```", inline=False)
        review_embed.timestamp = datetime.datetime.utcnow()

        await review_channel.send(embed=review_embed, view=AppealReviewButtons())
        await interaction.followup.send("✅ **Success!** Your system appeal file has been dispatched.", ephemeral=True)

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
            await target_user.send(embed=discord.Embed(title="✅ Appeal Approved", description=f"Your appeal for Case ID `{case_id}` has been accepted. The infraction has been lifted.", color=discord.Color.green()))
        except Exception: pass

        embed.color = discord.Color.green()
        embed.title = "✅ Appeal Cleared & Approved"
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

        embed.color = discord.Color.red()
        embed.title = "❌ Appeal Evaluated & Rejected"
        embed.set_footer(text=f"Rejected upon review by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

# ── Universal Punishment Lifter ──────────────────────────────────────────────
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
        saved_ids = pop_suspended_roles(uid)
        if not saved_ids: return "Staff Suspension lifted (No backups found)"
        try:
            member = await guild.fetch_member(uid)
            if member:
                restored = 0
                for r_id in saved_ids:
                    role = guild.get_role(r_id)
                    if role:
                        if role.name.strip() in PROTECTED_ROLE_NAMES: continue
                        try:
                            await member.add_roles(role)
                            restored += 1
                        except Exception: pass
                return f"Staff Suspension lifted ({restored} roles restored)"
        except Exception as e: return f"Staff Suspension role error: {e}"

    return "Database trail flagged"

# ── Master Mod Action Engine ──────────────────────────────────────────────────
async def run_moderation_action(interaction: discord.Interaction, user: discord.Member, reason: str, restriction_type: str, source: str, end_date: str, timeout_duration: datetime.timedelta = None):
    warning_id = str(uuid.uuid4())[:8].upper()
    timestamp  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date = timestamp[:10]
    final_expiry = end_date if end_date else "Never"

    roblox_username = user.nick if user.nick else user.display_name
    execution_notes = "Logged to Database"

    # Pre-dispatch DM notice card BEFORE running the action loop
    dm_embed = discord.Embed(
        title=f"⚠️ Account Moderation Notice: {restriction_type.upper()}",
        description=f"A formal system action has been registered against your account profile inside **{interaction.guild.name}** due to a rules violation.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    dm_embed.add_field(name="📋 Infraction Type", value=restriction_type, inline=True)
    dm_embed.add_field(name="📋 Stated Reason", value=f"```text\n{reason}\n```", inline=False)
    dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Platform context", value=source, inline=True)
    dm_embed.add_field(name="Expiration Date", value=final_expiry, inline=True)
    dm_embed.add_field(name="⚖️ External Appeal Request Notice", value=f"If you are restricted, submit a review here:\n📋 **[Open Google Appeal Form]({GOOGLE_APPEAL_FORM_URL})**", inline=False)
    dm_embed.set_footer(text="Automated Compliance Engine • Busways Administration")
    dm_embed.timestamp = datetime.datetime.utcnow()

    dm_sent = await dm_user(user, dm_embed)

    if source == "Discord":
        if restriction_type == "Timeout":
            if timeout_duration:
                try:
                    await user.timeout(timeout_duration, reason=reason)
                    execution_notes = f"Timed out natively via duration utility"
                except Exception as e: execution_notes = f"Logged (Timeout failed: {e})"
            else:
                try:
                    await user.timeout(datetime.timedelta(days=1), reason=reason)
                    execution_notes = "Timed out for 24 Hours (Default)"
                    final_expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception as e: execution_notes = f"Logged (Timeout failed: {e})"

        elif restriction_type == "Ban":
            try:
                await user.ban(delete_message_days=1, reason=reason)
                execution_notes = "Banned cleanly from guild instance"
            except Exception as e: execution_notes = f"Logged (API Ban execution failed: {e})"

    append_row([
        str(user.id), roblox_username, str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", source, restriction_type, start_date, final_expiry, warning_id
    ])

    embed = discord.Embed(title=f"🛑 User Log Added ({restriction_type})", description=f"A formal {restriction_type.lower()} record has been generated and securely logged to the central database.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User", value=f"{user.mention}\n`ID: {user.id}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Type", value=f"**{restriction_type}**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="API Execution", value=f"`{execution_notes}`", inline=True)
    embed.add_field(name="Expiry Date", value=final_expiry, inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Dispatched Before Action" if dm_sent else "❌ Closed DMs", inline=True)
    embed.set_footer(text=f"To undo this record file, execute: /revokeaction {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await interaction.followup.send(embed=embed)

# ── Slash Commands ─────────────────────────────────────────────────────────────
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

    embed = discord.Embed(title="Universal Revoke Executed", color=discord.Color.from_rgb(39, 174, 96))
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
    user_infractions = {}
    admin_actions = {}

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

@bot.tree.command(name="viewmywarnings", description="View all your warnings split between Discord and Roblox (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    warnings = get_user_warnings(user_id)
    embed = discord.Embed(title="User Moderation History", description="Your logs are categorized below based on where the infraction occurred.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    if not warnings:
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)
        
    txt = ""
    for r, _ in warnings:
        txt += f"▪ McKay: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Context: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        
    embed.add_field(name=f"📊 Your Cataloged History Files ({len(warnings)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user split by platform")
@app_commands.describe(user_id="The clean numerical User ID to evaluate (e.g. 1476833993671446628)")
async def viewwarnings(interaction: discord.Interaction, user_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=False)
    warnings = get_user_warnings(user_id.strip())
    
    embed = discord.Embed(title=f"User Log File — Database Search Results", description=f"Displaying data array matching target unique account identifier: `{user_id.strip()}`", color=discord.Color.from_rgb(44, 62, 80))
    if not warnings:
        embed.add_field(name="📋 Record Status", value="```text\nNo tracking rows matched this unique target numerical identifier inside our system records.\n```", inline=False)
        return await interaction.followup.send(embed=embed)
        
    txt = ""
    for r, _ in warnings:
        txt += f"▪ McKay: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Context: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        
    embed.add_field(name=f"📊 Cataloged History Files ({len(warnings)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="timeout", description="[Admin] Time out a user natively and log to sheet")
@app_commands.describe(user="The user member profile", reason="Reason for timeout", duration_amount="The number value for length", duration_unit="The unit of measurement", source="Platform context")
@app_commands.choices(
    source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")],
    duration_unit=[app_commands.Choice(name="Minutes", value="minutes"), app_commands.Choice(name="Hours", value="hours"), app_commands.Choice(name="Days", value="days")]
)
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, duration_amount: int, duration_unit: app_commands.Choice[str], source: app_commands.Choice[str] = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    if duration_amount <= 0: return await interaction.followup.send("❌ **Error:** Duration must be a positive integer.")
        
    unit = duration_unit.value
    if unit == "minutes": delta = datetime.timedelta(minutes=duration_amount)
    elif unit == "hours": delta = datetime.timedelta(hours=duration_amount)
    else: delta = datetime.timedelta(days=duration_amount)
    
    expiry_time = datetime.datetime.utcnow() + delta
    final_expiry_stamp = expiry_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, user, reason, "Timeout", source.value if source else "Discord", final_expiry_stamp, timeout_duration=delta)

@bot.tree.command(name="ban", description="[Admin] Ban a user natively from the server and log to sheet")
@app_commands.describe(user="The user member profile", reason="Reason for ban", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def ban_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Ban", source.value if source else "Discord", end_date)

@bot.tree.command(name="staff_suspension", description="[Admin] Suspend a staff member and strip non-protected roles")
@app_commands.describe(user="The staff member profile", reason="Reason for suspension", source="Platform context", end_date="Expiry date (YYYY-MM-DD) REQUIRED")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str, source: app_commands.Choice[str] = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    try: datetime.datetime.strptime(end_date.strip(), "%Y-%m-%d")
    except ValueError: return await interaction.followup.send("❌ **Error:** Expiry date format required: `YYYY-MM-DD`")

    role_ids = [r.id for r in user.roles if r.name != "@everyone" and not r.managed]
    save_suspended_roles(user.id, role_ids)
    for role in user.roles:
        if role.name != "@everyone" and not role.managed:
            if role.name.strip() in PROTECTED_ROLE_NAMES: continue
            try: await user.remove_roles(role)
            except Exception: pass
    await run_moderation_action(interaction, user, reason, "Staff Suspension", source.value if source else "Discord", end_date)

@bot.tree.command(name="restoreroles", description="Restore original roles if your suspension has expired")
async def restoreroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    history = get_user_warnings(str(user_id))
    suspensions = [r for r, _ in history if pad(r)[COL_RESTRICTION].strip() == "Staff Suspension" and pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not suspensions: return await interaction.followup.send("❌ No active Staff Suspension logs.", ephemeral=True)

    active_suspension = pad(suspensions[-1])
    expiry_str = active_suspension[COL_END_DATE].strip()
    if expiry_str == "Never": return await interaction.followup.send("🔒 Suspension is marked as Permanent.", ephemeral=True)

    try:
        if datetime.datetime.strptime(expiry_str, "%Y-%m-%d") > datetime.datetime.utcnow():
            return await interaction.followup.send(f"⏳ Suspension active until `{expiry_str}`.", ephemeral=True)
    except ValueError: return await interaction.followup.send("❌ Format corrupted on sheet.", ephemeral=True)

    saved_ids = pop_suspended_roles(user_id)
    if not saved_ids: return await interaction.followup.send("⚠️ Backup file missing.", ephemeral=True)

    restored = 0
    for r_id in saved_ids:
        role = interaction.guild.get_role(r_id)
        if role:
            if role.name.strip() in PROTECTED_ROLE_NAMES: continue
            try: await interaction.user.add_roles(role); restored += 1
            except Exception: pass

    for r, idx in history:
        if pad(r)[COL_INCIDENT_ID] == active_suspension[COL_INCIDENT_ID]:
            r_padded = pad(r)
            r_padded[COL_REVOKED], r_padded[COL_REVOKED_BY], r_padded[COL_REVOKED_AT] = "TRUE", "System Auto-Restore", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx, r_padded)
            break
    await interaction.followup.send(f"✅ Restored **{restored}** staff roles cleanly.", ephemeral=True)

# ── Production Flask Engine Server ─────────────────────────────────────────────
app = Flask(__name__)
@app.route('/')
def home(): return "BWR7 Warnings Bot is Online Framework Stable!", 200

def run_web_server(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
def run_discord_bot():
    if not DISCORD_TOKEN: return
    bot.run(DISCORD_TOKEN)

print("🛰️ Initializing multi-threaded background pipelines...")
threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=run_discord_bot, daemon=True).start()
