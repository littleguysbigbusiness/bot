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
STATIC_STATUS_ID  = 1505559844449419284  
APPEAL_CHANNEL_ID = 1420690312531017850  

# Your official application link assets
BOT_INVITE_URL         = "https://discord.com/oauth2/authorize?client_id=1476833993671446628"
GOOGLE_APPEAL_FORM_URL = "https://forms.google.com"

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

# ── Google Authentication ──────────────────────────────────────────────────────
from google.oauth2 import service_account
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = None

if os.path.exists("service_account.json"):
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json", scopes=SCOPES
    )

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
STATUS_EMOJI = {
    "operational": "✅",
    "degraded_performance": "🟨",
    "partial_outage": "🟧",
    "major_outage": "🔴",
    "under_maintenance": "🔵",
    "unknown": "⬜"
}
STATUS_COLOR = {
    "none": discord.Color.green(),
    "minor": discord.Color.yellow(),
    "major": discord.Color.red(),
    "critical": discord.Color.red(),
    "maintenance": discord.Color.blue(),
    "unknown": discord.Color.light_grey()
}

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
            except Exception:
                pass
    except Exception:
        pass

# ── Interactive Appeal System Views ───────────────────────────────────────────
class AppealReasonModal(discord.ui.Modal, title="Submit Case File Appeal"):
    appeal_reason = discord.ui.TextInput(
        label="Why should this infraction be removed?",
        style=discord.TextStyle.paragraph,
        placeholder="Provide clean operational context or evidence for review...",
        required=True,
        max_length=500
    )

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

        review_embed = discord.Embed(
            title="📥 System Infraction Appeal Submitted",
            description="User has requested a file evaluation regarding an active system restriction.",
            color=discord.Color.from_rgb(230, 126, 34)
        )
        review_embed.add_field(name="👤 Appellant", value=f"{interaction.user.mention}\n`ID: {interaction.user.id}`", inline=True)
        review_embed.add_field(name="🆔 Target Case ID", value=f"`{self.case_id}`", inline=True)
        review_embed.add_field(name="📊 Action Type", value=self.restriction_type, inline=True)
        review_embed.add_field(name="📋 Original Reason Cell", value=f"```text\n{self.original_reason}\n```", inline=False)
        review_embed.add_field(name="💬 Appellant Statement", value=f"```text\n{self.appeal_reason.value}\n```", inline=False)
        review_embed.timestamp = datetime.datetime.utcnow()

        await review_channel.send(embed=review_embed, view=AppealReviewButtons())
        await interaction.followup.send("✅ **Success!** Your system appeal file has been dispatched for active review.", ephemeral=True)

class AppealDropdownMenu(discord.ui.Select):
    def __init__(self, user_active_cases):
        options = []
        for row, _ in user_active_cases:
            case_id = row[COL_INCIDENT_ID].strip()
            rest_type = row[COL_RESTRICTION].strip()
            reason = row[COL_REASON].strip()
            options.append(discord.SelectOption(
                label=f"Case: {case_id} [{rest_type}]",
                value=case_id,
                description=reason[:50]
            ))
        super().__init__(placeholder="Select an active infraction file to appeal...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        case_id = self.values[0]
        row, _ = find_warning_by_id(case_id)
        if not row:
            return await interaction.response.send_message("❌ Case resolved or moved.", ephemeral=True)
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
        if not is_admin(interaction):
            return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
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
        except Exception:
            pass

        embed.color = discord.Color.green()
        embed.title = "✅ Appeal Cleared & Approved"
        embed.set_footer(text=f"Approved and lifted by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(label="Deny Appeal", style=discord.ButtonStyle.danger, custom_id="deny_appeal_btn", emoji="🔴")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer()
        embed = interaction.message.embeds[0]
        case_id = embed.fields[1].value.replace("`", "").strip()
        appellant_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        try:
            target_user = await bot.fetch_user(appellant_id)
            await target_user.send(embed=discord.Embed(title="❌ Appeal Denied", description=f"Your appeal for Case ID `{case_id}` has been rejected.", color=discord.Color.red()))
        except Exception:
            pass

        embed.color = discord.Color.red()
        embed.title = "❌ Appeal Evaluated & Rejected"
        embed.set_footer(text=f"Rejected upon review by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

# ── Universal Punishment Lifter ──────────────────────────────────────────────
async def execute_live_punishment_revocation(guild: discord.Guild, row, admin_name: str) -> str:
    uid = int(row[COL_USER_ID].strip())
    rest_type = row[COL_RESTRICTION].strip()
    source_context = row[COL_SOURCE].strip()

    if source_context != "Discord":
        return "Logged to Sheet (In-Game Context)"

    if rest_type == "Timeout":
        try:
            member = await guild.fetch_member(uid)
            if member:
                await member.timeout(None, reason=f"Universal Revoke executed by {admin_name}")
                return "Timeout lifted cleanly (User Unmuted)"
        except Exception as e:
            return f"Timeout lift error: {e}"

    elif rest_type == "Ban":
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Universal Revoke executed by {admin_name}")
            return "Ban successfully lifted (User Unbanned)"
        except Exception as e:
            return f"API Unban execution failed: {e}"

    elif rest_type == "Staff Suspension":
        saved_ids = pop_suspended_roles(uid)
        if not saved_ids:
            return "Staff Suspension lifted (No backups found)"
        try:
            member = await guild.fetch_member(uid)
            if member:
                restored = 0
                for r_id in saved_ids:
                    role = guild.get_role(r_id)
                    if role:
                        try:
                            await member.add_roles(role)
                            restored += 1
                        except Exception:
                            pass
                return f"Staff Suspension lifted ({restored} roles restored)"
        except Exception as e:
            return f"Staff Suspension role allocation error: {e}"

    return "Database trail flagged"

# ── Master Mod Action Engine ──────────────────────────────────────────────────
async def run_moderation_action(interaction: discord.Interaction, user: discord.Member, reason: str, restriction_type: str, source: str, end_date: str):
    warning_id = str(uuid.uuid4())[:8].upper()
    timestamp  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date = timestamp[:10]
    final_expiry = end_date if end_date else "Never"

    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    execution_notes = "Logged to Database"
    
    if source == "Discord":
        if restriction_type == "Timeout":
            if end_date:
                try:
                    target_expiry = datetime.datetime.strptime(end_date.strip(), "%Y-%m-%d")
                    duration = target_expiry - datetime.datetime.utcnow()
                    if duration.total_seconds() > 0:
                        await user.timeout(duration, reason=reason)
                        execution_notes = f"Timed out until {end_date}"
                    else:
                        execution_notes = "Logged (Expiry date is historical)"
                except Exception as e:
                    execution_notes = f"Logged (Timeout failed: {e})"
            else:
                try:
                    await user.timeout(datetime.timedelta(days=1), reason=reason)
                    execution_notes = "Timed out for 24 Hours (Default)"
                    final_expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception as e:
                    execution_notes = f"Logged (Timeout failed: {e})"

        elif restriction_type == "Ban":
            try:
                await user.ban(delete_message_days=1, reason=reason)
                execution_notes = "Banned cleanly from guild instance"
            except Exception as e:
                execution_notes = f"Logged (API Ban execution failed: {e})"

    append_row([
        str(user.id), str(user), str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", source, restriction_type, start_date, final_expiry, warning_id
    ])

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
    
    form_instructions = (
        "If you are banned or restricted from server channels and cannot submit an in-app `/appeal`, "
        f"you may lodge an external case evaluation request via our primary appeal form:\n"
        f"📋 **[Click Here to Open Google Appeal Form]({GOOGLE_APPEAL_FORM_URL})**\n\n"
        "To review bot deployment status or invite properties externally, use this link:\n"
        f"🛰️ **[Busways Assistance Application Invite Gateway]({BOT_INVITE_URL})**"
    )
    dm_embed.add_field(name="⚖️ External Appeal Request Notice", value=form_instructions, inline=False)
    dm_embed.set_footer(text="Automated Compliance Engine • Busways Administration")
    dm_embed.timestamp = datetime.datetime.utcnow()

    dm_sent = await dm_user(user, dm_embed)

    embed = discord.Embed(title=f"🛑 User Log Added ({restriction_type})", description=f"A formal {restriction_type.lower()} record has been generated and securely logged to the central database.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User", value=f"{user.mention}\n`ID: {user.id}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Type", value=f"**{restriction_type}**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="API Execution", value=f"`{execution_notes}`", inline=True)
    embed.add_field(name="Expiry Date", value=final_expiry, inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Dispatched Successfully" if dm_sent else "❌ Closed DMs", inline=True)
    embed.set_footer(text=f"To undo this record file, execute: /revokeaction {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await interaction.followup.send(embed=embed)

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.command(name="revokeaction", description="[Admin] Revoke an active moderation file and instantly lift its punishment")
@app_commands.describe(case_id="The Case ID to revoke (e.g. AB12CD34)")
async def revokeaction(interaction: discord.Interaction, case_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    row, sheet_row = find_warning_by_id(case_id)
    if row is None:
        return await interaction.followup.send(f"❌ Case ID `{case_id}` not found on database sheet.")
    if row[COL_REVOKED].strip().upper() == "TRUE":
        return await interaction.followup.send(f"⚠️ Case file `{case_id}` has already been revoked.")

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
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    rows = read_all_rows()
    if not rows:
        return await interaction.followup.send("📋 Database empty.")

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

        if rest_type in type_counts:
            type_counts[rest_type] += 1
        if not is_revoked and uid:
            user_key = f"<@{uid}> (`{username}`)"
            user_infractions[user_key] = user_infractions.get(user_key, 0) + 1
        if admin_name:
            admin_actions[admin_name] = admin_actions.get(admin_name, 0) + 1

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
    if not active_cases:
        return await interaction.followup.send("✅ You have no active warnings or restrictions available to appeal!", ephemeral=True)
    await interaction.followup.send("📋 **Infraction System Appeal Port:**\nSelect the case file from the dropdown:", view=AppealDropdownView(active_cases), ephemeral=True)

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice
