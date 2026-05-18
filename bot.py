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

# ── Google Service Account Authentication ─────────────────────────────────────
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

# ── Roles JSON Backup Helpers ──────────────────────────────────────────────────
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
    appeal_reason = discord.ui.TextInput(label="Why should this infraction be removed?", style=discord.TextStyle.paragraph, placeholder="Provide clean operational context or evidence for review...", required=True, max_length=500)

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
        await interaction.followup.send("✅ **Success!** Your system appeal file has been dispatched to corporate administration for active review.", ephemeral=True)

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
        if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer()
        embed = interaction.message.embeds[0]
        case_id = embed.fields[1].value.replace("`", "").strip()
        appellant_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        row, sheet_row = find_warning_by_id(case_id)
        if row and row[COL_REVOKED].strip().upper() != "TRUE":
            row[COL_REVOKED]    = "TRUE"
            row[COL_REVOKED_BY] = f"Appeal Appr: {interaction.user}"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(sheet_row, row)

        try:
            target_user = await bot.fetch_user(appellant_id)
            await target_user.send(embed=discord.Embed(title="✅ Appeal Approved", description=f"Your appeal for Case ID `{case_id}` has been accepted. The infraction has been expunged.", color=discord.Color.green()))
        except Exception: pass

        embed.color, embed.title = discord.Color.green(), "✅ Appeal Cleared & Approved"
        embed.set_footer(text=f"Approved by {interaction.user}")
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

# ── Universal Action Master Engine (Now Handles Live API Penalties) ───────────
async def run_moderation_action(interaction: discord.Interaction, user: discord.Member, reason: str, restriction_type: str, source: str, end_date: str):
    warning_id = str(uuid.uuid4())[:8].upper()
    timestamp  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date = timestamp[:10]
    final_expiry = end_date if end_date else "Never"

    all_warnings = get_user_warnings(str(user.id))
    active_count = sum(1 for r, _ in all_warnings if pad(r)[COL_REVOKED].upper() != "TRUE") + 1

    # ── LIVE NATIVE API ENFORCEMENTS ──
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
                # Default safety fallback mute duration if field omitted
                try:
                    await user.timeout(datetime.timedelta(days=1), reason=reason)
                    execution_notes = "Timed out for 24 Hours (Default)"
                    final_expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception as e: execution_notes = f"Logged (Timeout failed: {e})"

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

    dm_embed = discord.Embed(title=f"⚠️ Account Moderation Action: {restriction_type.upper()}", description=f"A formal system action has been registered for your account in **{interaction.guild.name}**.", color=discord.Color.from_rgb(44, 62, 80))
    dm_embed.add_field(name="📋 Type", value=restriction_type, inline=True)
    dm_embed.add_field(name="📋 Reason", value=f"```text\n{reason}\n```", inline=False)
    dm_embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    dm_embed.add_field(name="Platform", value=source, inline=True)
    dm_embed.add_field(name="Expires", value=final_expiry, inline=True)
    dm_embed.set_footer(text="Automated Compliance Engine")
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
    embed.add_field(name="DM Delivery", value="✅ Dispatched" if dm_sent else "❌ Closed DMs", inline=True)
    embed.set_footer(text=f"To undo this record file, execute: /revokewarning {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await interaction.followup.send(embed=embed)

# ── Server Wide Statistics Command ─────────────────────────────────────────────
@bot.tree.command(name="modstats", description="[Admin] View server-wide moderation metrics and leaderboard layout")
async def modstats(interaction: discord.Interaction):
    if not is_admin(interaction): 
        return await interaction.response.send_message("❌ Administrative clearance verification required.", ephemeral=True)
    
    await interaction.response.defer()
    rows = read_all_rows()
    
    if not rows:
        await interaction.followup.send("📋 **Database Empty:** No operational infraction logs found on the spreadsheet tracking grid.")
        return

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

        # Track user counts (only active ones count toward target record holder)
        if not is_revoked and uid:
            user_key = f"<@{uid}> (`{username}`)"
            user_infractions[user_key] = user_infractions.get(user_key, 0) + 1

        # Track admin actions (all actions logged count)
        if admin_name:
            admin_actions[admin_name] = admin_actions.get(admin_name, 0) + 1

    # Resolve Leaderboards
    top_user = "None"
    max_user_count = 0
    if user_infractions:
        top_user_key = max(user_infractions, key=user_infractions.get)
        max_user_count = user_infractions[top_user_key]
        top_user = f"{top_user_key} — **{max_user_count}** active cases"

    top_admin = "None"
    max_admin_count = 0
    if admin_actions:
        top_admin_key = max(admin_actions, key=admin_actions.get)
        max_admin_count = admin_actions[top_admin_key]
        top_admin = f"👤 **{top_admin_key}** — **{max_admin_count}** actions logged"

    embed = discord.Embed(
        title="📊 Server Moderation Analytics Overview",
        description="Centralized data insight compiled dynamically from the primary compliance log tracking sheet.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    
    breakdown_text = (
        f"⚠️ **Warnings:** {type_counts['Warning']}\n"
        f"⏳ **Timeouts:** {type_counts['Timeout']}\n"
        f"🔨 **Bans:** {type_counts['Ban']}\n"
        f"🛡️ **Staff Suspensions:** {type_counts['Staff Suspension']}"
    )
    
    embed.add_field(name="📈 Metrics Scale", value=f"Total Records Logged: **{total_logs}**", inline=False)
    embed.add_field(name="🗂️ Action Type Breakdown", value=breakdown_text, inline=True)
    embed.add_field(name="🚨 Highest Active Infractions User", value=top_user, inline=False)
    embed.add_field(name="👮 Top Enforcing Administrator", value=top_admin, inline=False)
    embed.set_footer(text="Busways Operational Insights Control Panel")
    embed.timestamp = datetime.datetime.utcnow()

    await interaction.followup.send(embed=embed)

# ── Moderation Commands ────────────────────────────────────────────────────────
@bot.tree.command(name="appeal", description="Submit an evaluation appeal form for an active infraction file")
async def appeal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    active_cases = [(r, idx) for r, idx in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not active_cases: return await interaction.followup.send("✅ You have no active warnings or restrictions available to appeal!", ephemeral=True)
    await interaction.followup.send("📋 **Infraction System Appeal Port:**\nSelect the specific Case ID file you want to appeal from the dropdown below:", view=AppealDropdownView(active_cases), ephemeral=True)

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user to warn", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="timeout", description="[Admin] Time out a user natively and log to sheet")
@app_commands.describe(user="The user to mute", reason="Reason for timeout", source="Platform context", end_date="Expiry date (YYYY-MM-DD) e.g. 2026-05-25")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Timeout", source.value if source else "Discord", end_date)

@bot.tree.command(name="ban", description="[Admin] Ban a user natively from the server and log to sheet")
@app_commands.describe(user="The user to ban", reason="Reason for ban", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def ban_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, user, reason, "Ban", source.value if source else "Discord", end_date)

@bot.tree.command(name="staff_suspension", description="[Admin] Suspend a staff member and strip roles")
@app_commands.describe(user="The staff member", reason="Reason for suspension", source="Platform context", end_date="Expiry date (YYYY-MM-DD) REQUIRED")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str, source: app_commands.Choice[str] = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    try: datetime.datetime.strptime(end_date.strip(), "%Y-%m-%d")
    except ValueError: return await interaction.followup.send("❌ **Error:** Expiry date must match `YYYY-MM-DD` formatting.")

    role_ids = [r.id for r in user.roles if r.name != "@everyone" and not r.managed]
    save_suspended_roles(user.id, role_ids)
    for role in user.roles:
        if role.name != "@everyone" and not role.managed:
            try: await user.remove_roles(role)
            except Exception: pass
    await run_moderation_action(interaction, user, reason, "Staff Suspension", source.value if source else "Discord", end_date)

# ── Role Restoration Logic ─────────────────────────────────────────────────────
@bot.tree.command(name="restoreroles", description="Restore original roles if your suspension has expired")
async def restoreroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    history = get_user_warnings(str(user_id))
    suspensions = [r for r, _ in history if pad(r)[COL_RESTRICTION].strip() == "Staff Suspension" and pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not suspensions: return await interaction.followup.send("❌ You have no active Staff Suspension logs.", ephemeral=True)

    active_suspension = pad(suspensions[-1])
    expiry_str = active_suspension[COL_END_DATE].strip()
    if expiry_str == "Never": return await interaction.followup.send("🔒 Your suspension is marked as Permanent.", ephemeral=True)

    try:
        if datetime.datetime.strptime(expiry_str, "%Y-%m-%d") > datetime.datetime.utcnow():
            return await interaction.followup.send(f"⏳ **Access Denied:** Suspension is active until `{expiry_str}`.", ephemeral=True)
    except ValueError: return await interaction.followup.send("❌ Expiry date format corrupted on database sheet.", ephemeral=True)

    saved_ids = pop_suspended_roles(user_id)
    if not saved_ids: return await interaction.followup.send("⚠️ Original role backup file not found.", ephemeral=True)

    restored = 0
    for r_id in saved_ids:
        role = interaction.guild.get_role(r_id)
        if role:
            try: await interaction.user.add_roles(role); restored += 1
            except Exception: pass

    for r, idx in history:
        if pad(r)[COL_INCIDENT_ID] == active_suspension[COL_INCIDENT_ID]:
            r_padded = pad(r)
            r_padded[COL_REVOKED], r_padded[COL_REVOKED_BY], r_padded[COL_REVOKED_AT] = "TRUE", "System Auto-Restore", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx, r_padded)
            break
    await interaction.followup.send(f"✅ **Success!** Restored **{restored}** original staff roles cleanly.", ephemeral=True)

# ── General Lookups ────────────────────────────────────────────────────────────
@bot.tree.command(name="revokewarning", description="[Admin] Revoke an action by its Case ID")
@app_commands.describe(warning_id="Case ID to revoke")
async def revokewarning(interaction: discord.Interaction, warning_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    row, sheet_row = find_warning_by_id(warning_id)
    if row is None: return await interaction.followup.send(f"❌ Case ID `{warning_id}` not found.")
    if row[COL_REVOKED].strip().upper() == "TRUE": return await interaction.followup.send(f"⚠️ Case `{warning_id}` already revoked.")

    row[COL_REVOKED], row[COL_REVOKED_BY], row[COL_REVOKED_AT] = "TRUE", str(interaction.user), datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    update_row(sheet_row, row)

    embed = discord.Embed(title="✅ User Log Updated", color=discord.Color.from_rgb(39, 174, 96))
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="User File", value=f"<@{row[COL_USER_ID]}>", inline=True)
    embed.add_field(name="Status Update", value="⚪ **Removed / Revoked**", inline=True)
    embed.add_field(name="Original Reason", value=row[COL_REASON], inline=False)
    embed.add_field(name="Revoked By", value=interaction.user.mention, inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="viewmywarnings", description="View all your warnings (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    if not warnings:
        embed = discord.Embed(title="User Moderation History", description="This menu is where you can view your past and current moderation history.", color=discord.Color.from_rgb(44, 62, 80))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]
    embed = discord.Embed(title="User Moderation History", description="This menu is where you can view your past and current moderation history.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    if active:
        txt = ""
        for r, _ in active: txt += f"▪️ ID: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Source: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        embed.add_field(name=f"🟢 Active Records ({len(active)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    if revoked:
        txt = ""
        for r, _ in revoked: txt += f"▪️ ID: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | {r[COL_REASON]} (Revoked)\n"
        embed.add_field(name=f"⚪ Historical Archive ({len(revoked)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user")
@app_commands.describe(user="The user to look up")
async def viewwarnings(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(user.id))
    if not warnings:
        embed = discord.Embed(title=f"User Log File — {user.display_name}", description="This menu is where you can view all past and current moderation history.", color=discord.Color.from_rgb(44, 62, 80))
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="📋 Record Status", value="```text\nNo records found. Thank you for abiding by the rules!\n```", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)

    active  = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    revoked = [(r, n) for r, n in warnings if pad(r)[COL_REVOKED].upper() == "TRUE"]
    embed = discord.Embed(title=f"User Log File — {user.display_name}", description="This menu is where you can view all past and current moderation history.", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)

    if active:
        txt = ""
        for r, _ in active: txt += f"▪️ Case: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | Source: {r[COL_SOURCE]}\n  Reason: {r[COL_REASON]}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE]}\n\n"
        embed.add_field(name=f"🟢 Active Records ({len(active)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    if revoked:
        txt = ""
        for r, _ in revoked: txt += f"▪️ Case: {r[COL_INCIDENT_ID]} | Type: {r[COL_RESTRICTION]} | {r[COL_REASON]} (Revoked)\n"
        embed.add_field(name=f"⚪ Historical Archive ({len(revoked)})", value=f"```text\n{txt.strip()}\n```", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

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
