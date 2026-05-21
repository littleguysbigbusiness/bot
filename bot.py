import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import uuid
import datetime
import requests
import threading
import json
import re
import asyncio
from flask import Flask, request, jsonify, render_template_string

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
VERIFY_SHEET_NAME = "VerifiedUsers"  
STATE_SHEET_NAME  = "TempStates"  
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1476812926521184276  
STATIC_STATUS_ID  = 1505808587807789117  
APPEAL_CHANNEL_ID = 1505891264032149574  

# 🔐 Roblox Official OAuth2 Credentials
ROBLOX_CLIENT_ID     = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI  = "https://bot-h57e.onrender.com/roblox_callback" 

# Your official application appeal assets
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

VERIFY_READ_URL   = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET_NAME}!A:C"
VERIFY_APPEND_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{VERIFY_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

STATE_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET_NAME}!A:C"
STATE_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{STATE_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

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

# ── Google Authentication ──────────────────────────────────────────────────────
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

# ── Utilities ──────────────────────────────────────────────────────────────────
def extract_id(input_string: str) -> str:
    match = re.search(r'\d+', input_string)
    return match.group(0) if match else input_string.strip()

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

# ── Verification Database Helpers ──────────────────────────────────────────────
def log_verified_user(discord_id: str, roblox_id: str, roblox_username: str):
    try:
        payload = {"values": [[str(discord_id), str(roblox_id), str(roblox_username)]]}
        requests.post(VERIFY_APPEND_URL, headers=sheets_headers(), json=payload, timeout=10)
    except Exception as e:
        print(f"[Verification Sheet Log Error] {e}")

def get_verified_roblox_id(discord_id: str) -> str:
    try:
        resp = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        if len(rows) > 1:
            for row in rows[1:]:
                if row and row[0].strip() == str(discord_id).strip():
                    return row[1].strip()
    except Exception: pass
    return None

# ── Cloud Token State Persistence Helpers ──────────────────────────────────────
def save_oauth_state_to_cloud(state_token: str, discord_user_id: int):
    try:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        payload = {"values": [[str(state_token), str(discord_user_id), ts]]}
        requests.post(STATE_APPEND_URL, headers=sheets_headers(), json=payload, timeout=10)
    except Exception as e:
        print(f"[Cloud State Appending Error] {e}")

def pop_oauth_state_from_cloud(state_token: str) -> str:
    try:
        resp = requests.get(STATE_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        if not rows or len(rows) <= 1:
            return None
            
        for i, row in enumerate(rows[1:]):
            if row and row[0].strip() == str(state_token).strip():
                discord_id = row[1].strip()
                clear_range = f"{STATE_SHEET_NAME}!A{i+2}:C{i+2}"
                clear_url = f"{SHEET_UPDATE_BASE}{clear_range}?valueInputOption=RAW"
                requests.put(clear_url, headers=sheets_headers(), json={"values": [["", "", ""]]}, timeout=5)
                return discord_id
    except Exception as e:
        print(f"[Cloud State Evaluation Error] {e}")
    return None

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
        await self.tree.sync()
        print("✅ Slash commands synced globally.")
    async def on_ready(self):
        print(f"✅ Logged in as {self.user}")
        if not update_status_embed.is_running():
            update_status_embed.start()
        if not automatic_expiry_sweeper.is_running():
            automatic_expiry_sweeper.start()

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

# ── Automated Background Expiry Engine (Runs Daily) ───────────────────────────
@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    print("[Sweeper] Starting automated infraction expiration analysis...")
    rows = read_all_rows()
    if not rows: return

    now = datetime.datetime.utcnow()
    current_date = now.date()

    for idx, raw_row in enumerate(rows):
        row = pad(raw_row)
        user_id_str = row[COL_USER_ID].strip()
        is_revoked = row[COL_REVOKED].strip().upper() == "TRUE"
        restriction_type = row[COL_RESTRICTION].strip()
        expiry_str = row[COL_END_DATE].strip()

        if is_revoked or not user_id_str or expiry_str in ("Never", "", "None"):
            continue

        try:
            clean_date_str = expiry_str.split(" ")[0].strip()
            expiry_date = datetime.datetime.strptime(clean_date_str, "%Y-%m-%d").date()
        except Exception: continue

        if current_date >= expiry_date:
            print(f"[Sweeper] Processing automatic termination for User ID: {user_id_str} [{restriction_type}]")
            for guild in bot.guilds:
                if restriction_type == "Ban":
                    try:
                        await guild.unban(discord.Object(id=int(user_id_str)), reason="System Auto-Expiry: Temporal duration limit exceeded.")
                    except Exception: pass
                elif restriction_type == "Timeout":
                    try:
                        member = await guild.fetch_member(int(user_id_str))
                        if member: await member.timeout(None, reason="System Auto-Expiry: Temporal duration limit exceeded.")
                    except Exception: pass

            row[COL_REVOKED] = "TRUE"
            row[COL_REVOKED] = "System Auto-Expiry"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx + 2, row)
            await asyncio.sleep(1)

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
        await interaction.message.defer()
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
async def run_moderation_action(interaction: discord.Interaction, target_id: str, target_name: str, target_member: discord.Member, reason: str, restriction_type: str, source: str, end_date: str, timeout_duration: datetime.timedelta = None):
    warning_id = str(uuid.uuid4())[:8].upper()
    timestamp  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start_date = timestamp[:10]
    final_expiry = end_date if end_date else "Never"
    execution_notes = "Logged to Database"

    dm_sent = False
    if target_member:
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
        dm_sent = await dm_user(target_member, dm_embed)

    if source == "Discord":
        if restriction_type == "Timeout" and target_member:
            if timeout_duration:
                try:
                    await target_member.timeout(timeout_duration, reason=reason)
                    execution_notes = f"Timed out natively via duration utility"
                except Exception as e: execution_notes = f"Logged (Timeout failed: {e})"
            else:
                try:
                    await target_member.timeout(datetime.timedelta(days=1), reason=reason)
                    execution_notes = "Timed out for 24 Hours (Default)"
                    final_expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception as e: execution_notes = f"Logged (Timeout failed: {e})"

        elif restriction_type == "Ban":
            try:
                await interaction.guild.ban(discord.Object(id=int(target_id)), delete_message_days=1, reason=reason)
                execution_notes = "Banned cleanly via user Object ID lookup"
            except Exception as e: execution_notes = f"Logged (API Ban execution failed: {e})"

    append_row([
        target_id, target_name, str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", source, restriction_type, start_date, final_expiry, warning_id
    ])

    embed = discord.Embed(title=f"🛑 User Log Added ({restriction_type})", description=f"A formal {restriction_type.lower()} record has been generated and securely logged to the central database.", color=discord.Color.from_rgb(44, 62, 80))
    if target_member: embed.set_thumbnail(url=target_member.display_avatar.url)
    embed.add_field(name="Target User Profile", value=f"<@{target_id}>\n`ID: {target_id}`\n`Name: {target_name}`", inline=True)
    embed.add_field(name="Case ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="Type", value=f"**{restriction_type}**", inline=True)
    embed.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    embed.add_field(name="API Execution", value=f"`{execution_notes}`", inline=True)
    embed.add_field(name="Expiry Date", value=final_expiry, inline=True)
    embed.add_field(name="Issuer", value=f"{interaction.user.mention}", inline=True)
    embed.add_field(name="DM Delivery", value="✅ Dispatched Before Action" if dm_sent else "❌ Closed DMs / Not in Guild", inline=True)
    embed.set_footer(text=f"To undo this record file, execute: /revokeaction {warning_id}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await interaction.followup.send(embed=embed)

# ── Display Layout Processing Engine ───────────────────────────────────────────
def build_historical_log_embed(title_text: str, warnings_list: list, thumbnail_url: str = None) -> discord.Embed:
    embed = discord.Embed(title=title_text, color=discord.Color.from_rgb(44, 62, 80))
    if thumbnail_url: embed.set_thumbnail(url=thumbnail_url)

    active_txt = ""
    revoked_txt = ""

    for r, _ in warnings_list:
        case_id = r[COL_INCIDENT_ID].strip()
        rest_type = r[COL_RESTRICTION].strip()
        context = r[COL_SOURCE].strip()
        reason = r[COL_REASON].strip()
        issued = r[COL_TIMESTAMP][:10]
        expires = r[COL_END_DATE].strip()
        is_revoked = r[COL_REVOKED].strip().upper() == "TRUE"

        log_block = f"▪ Case ID: {case_id} | Type: {rest_type} | Context: {context}\n  Reason: {reason}\n  Issued: {issued} | Expires: {expires}\n"
        
        if is_revoked:
            revoked_by = r[COL_REVOKED_BY].strip()
            log_block += f"  ❌ REVOKED BY: {revoked_by}\n\n"
            revoked_txt += log_block
        else:
            log_block += "\n"
            active_txt += log_block

    if not active_txt: active_txt = "No active infractions registered against this profile.\n"
    if not revoked_txt: revoked_txt = "No historical logs have been revoked or cleared.\n"

    embed.add_field(name="⚠️ Active Infractions & Restrictions", value=f"```text\n{active_txt.strip()}\n```", inline=False)
    embed.add_field(name="✅ Historical Archive (Revoked/Cleared Logs)", value=f"```text\n{revoked_txt.strip()}\n```", inline=False)
    embed.timestamp = datetime.datetime.utcnow()
    return embed

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.command(name="setprefix", description="Modify your server nickname layout with an organizational prefix tag")
@app_commands.describe(prefix="The prefix to add to your name (Max 5 letters. CEO/VCEO restricted)")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    clean_prefix = prefix.strip()
    
    # 🛑 Check 1: Length Validation
    if len(clean_prefix) > 5:
        return await interaction.followup.send("❌ **Validation Error:** Your prefix cannot be longer than **5 characters**.", ephemeral=True)
        
    # 🛑 Check 2: Blacklist Validation (Using regex to catch stealth variants like C.E.O or C-E-O)
    normalized_prefix = re.sub(r'[^A-Za-z0-9]', '', clean_prefix).upper()
    if normalized_prefix in ("CEO", "VCEO"):
        return await interaction.followup.send("❌ **Access Denied:** The prefixes **CEO** and **VCEO** are strictly reserved for executive management.", ephemeral=True)

    # Isolate baseline display name cleanly
    base_name = member.display_name
    if " - " in base_name:
        base_name = base_name.split(" - ")[1].strip()

    new_nickname = f"{clean_prefix} - {base_name}"
    
    if len(new_nickname) > 32:
        return await interaction.followup.send("❌ **Error:** Nickname string exceeds Discord's 32-character limit.", ephemeral=True)
        
    try:
        await member.edit(nick=new_nickname)
        await interaction.followup.send(f"✅ **Nickname configured:** Your name is now set to `{new_nickname}`", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ **API Error:** The bot is unable to modify your name. Ensure the bot's role is dragged higher in server hierarchy settings.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ **System Error:** Operation failed: `{e}`", ephemeral=True)

@bot.tree.command(name="verify", description="Link your Roblox account securely using the official Roblox Linking Prompt")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    
    if get_verified_roblox_id(user_id):
        return await interaction.followup.send("⚠️ **Account Linked Already:** Your profile is already registered in the database.", ephemeral=True)

    state_token = str(uuid.uuid4())
    save_oauth_state_to_cloud(state_token, user_id)

    roblox_oauth_url = (
        f"https://apis.roblox.com/oauth/v1/authorize"
        f"?client_id={ROBLOX_CLIENT_ID}"
        f"&redirect_uri={ROBLOX_REDIRECT_URI}"
        f"&scope=openid+profile"
        f"&response_type=code"
        f"&state={state_token}"
    )

    embed = discord.Embed(
        title="🔐 Official Roblox Account Verification",
        description="Click the official secure authorization link below to securely bind your Roblox coordinates directly to your server profile.",
        color=discord.Color.orange()
    )
    embed.add_field(name="📋 Verification Link", value=f"🔗 **[Click Here to Link Roblox Account]({roblox_oauth_url})**", inline=False)
    embed.set_footer(text="Powered by Official Roblox OAuth2 Security Framework")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="send_message", description="[Admin] Dispatch an announcement message or copy-paste an embed JSON structure into a channel")
@app_commands.describe(channel_id="The numerical unique ID of your target channel", message="Optional basic markdown text message content", embed_json="Optional copy-pasted embed code structured in valid JSON profile")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    
    cleaned_chan_id = extract_id(channel_id)
    target_channel = bot.get_channel(int(cleaned_chan_id))
    
    if not target_channel:
        return await interaction.followup.send(f"❌ **Error:** Target channel ID `{cleaned_chan_id}` could not be located inside this server footprint.")
    if not message and not embed_json:
        return await interaction.followup.send("❌ **Error:** You must provide either a text string `message` or a formatted `embed_json` block to transmit data.")

    target_embed = None
    if embed_json:
        try:
            clean_json = embed_json.strip()
            if clean_json.startswith("`" + "`" + "`json"): clean_json = clean_json[7:]
            elif clean_json.startswith("`" + "`" + "`"): clean_json = clean_json[3:]
            if clean_json.endswith("`" + "`" + "`"): clean_json = clean_json[:-3]
            
            data = json.loads(clean_json.strip())
            if "embeds" in data and isinstance(data["embeds"], list) and len(data["embeds"]) > 0:
                embed_dict = data["embeds"][0]
            else: embed_dict = data

            target_embed = discord.Embed.from_dict(embed_dict)
        except Exception as e:
            return await interaction.followup.send(f"❌ **JSON Formatting Error:** The compilation failed due to malformed data syntax.\n```text\n{e}\n```")

    try:
        await target_channel.send(content=message, embed=target_embed)
        await interaction.followup.send(f"✅ **Success!** Message package cleanly transmitted down onto <#{cleaned_chan_id}> context.")
    except discord.Forbidden:
        await interaction.followup.send(f"❌ **API Error:** The bot profile is missing permission fields required to write data inside <#{cleaned_chan_id}>.")
    except Exception as e:
        await interaction.followup.send(f"❌ **Transmission Error:** System failed to dispatch payload file.\n```text\n{e}\n```")

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
    
    embed = build_historical_log_embed("👤 Your Personal Compliance History", warnings, interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user split by platform")
@app_commands.describe(user_target="The numerical ID string or @mention of the target user profile")
async def viewwarnings(interaction: discord.Interaction, user_target: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=False)
    
    cleaned_id = extract_id(user_target)
    warnings = get_user_warnings(cleaned_id)
    
    thumb = None
    try:
        member = await interaction.guild.fetch_member(int(cleaned_id))
        if member: thumb = member.display_avatar.url
    except Exception:
        try:
            u_obj = await bot.fetch_user(int(cleaned_id))
            if u_obj: thumb = u_obj.display_avatar.url
        except Exception: pass

    embed = build_historical_log_embed(f"📋 Historical Audit File: {cleaned_id}", warnings, thumb)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", end_date)

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
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", source.value if source else "Discord", final_expiry_stamp, timeout_duration=delta)

@bot.tree.command(name="ban", description="[Admin] Ban a user from the server using their ID string or standard Mention")
@app_commands.describe(user_target="The 18-digit numerical ID or mention of the user to terminate", reason="Reason for ban", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def ban_cmd(interaction: discord.Interaction, user_target: str, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    
    cleaned_id = extract_id(user_target)
    target_member = None
    target_name = f"User_ID_{cleaned_id}"
    try:
        target_member = await interaction.guild.fetch_member(int(cleaned_id))
        if target_member: target_name = str(target_member)
    except Exception:
        try:
            user_obj = await bot.fetch_user(int(cleaned_id))
            if user_obj: target_name = str(user_obj)
        except Exception: pass

    clean_end_date = end_date.strip() if end_date else "Never"
    await run_moderation_action(interaction, cleaned_id, target_name, target_member, reason, "Ban", source.value if source else "Discord", clean_end_date)

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
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Staff Suspension", source.value if source else "Discord", end_date)

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

# ── Production Flask Engine Server & Roblox Secure OAuth2 Receiver ─────────────
app = Flask(__name__)

SUCCESS_HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Verification Complete</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #0f172a; color: #f8fafc; text-align: center; padding-top: 100px; }
        .card { background-color: #1e293b; max-width: 450px; margin: 0 auto; padding: 40px; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); border: 1px solid #334155; }
        h1 { color: #22c55e; margin-bottom: 10px; }
        p { color: #94a3b8; font-size: 16px; margin-bottom: 25px; }
        .footer { font-size: 12px; color: #475569; margin-top: 30px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>✅ Verification Success!</h1>
        <p>Your Roblox account identity <strong>{{ username }}</strong> has been securely linked to your Discord account footprint.</p>
        <span style="color: #64748b; font-size: 14px;">You can safely close this browser window tab now.</span>
        <div class="footer">Automated Compliance Engine • Busways Administration</div>
    </div>
</body>
</html>
"""

@app.route('/')
def home(): 
    return "BWR7 Warnings Bot is Online Framework Stable!", 200

@app.route('/privacy')
def privacy():
    return "Busways Verification App Privacy Policy: This application securely handles Roblox account identifiers solely for purpose linking server user metrics.", 200

@app.route('/terms')
def terms():
    return "Busways Verification App Terms of Service: By utilizing this verification portal, you authorize the application to verify your Roblox unique numeric identifier.", 200

@app.route('/roblox_callback')
def roblox_callback():
    auth_code = request.args.get("code")
    returned_state = request.args.get("state")

    if not auth_code or not returned_state:
        return "❌ Missing verification parameters from authorization gateway.", 400

    import time
    time.sleep(2)

    discord_user_id = pop_oauth_state_from_cloud(returned_state)
    if not discord_user_id:
        return "❌ Invalid anti-forgery request session state token expired.", 403

    try:
        token_url = "https://apis.roblox.com/oauth/v1/token"
        payload = {
            "client_id": ROBLOX_CLIENT_ID,
            "client_secret": ROBLOX_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": ROBLOX_REDIRECT_URI
        }
        
        token_resp = requests.post(token_url, data=payload, timeout=10)
        if token_resp.status_code != 200:
            return f"❌ Token request failure inside authorization channel: {token_resp.text}", 500

        access_token = token_resp.json().get("access_token")

        userinfo_url = "https://apis.roblox.com/oauth/v1/userinfo"
        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo_resp = requests.get(userinfo_url, headers=headers, timeout=10)
        
        if userinfo_resp.status_code != 200:
            return "❌ Failed fetching demographic profile array matching credentials.", 500

        user_info_data = userinfo_resp.json()
        roblox_id = user_info_data.get("sub") 
        roblox_name = user_info_data.get("preferred_username")

        log_verified_user(str(discord_user_id), str(roblox_id), roblox_name)

        async def send_dm_and_rename():
            try:
                for guild in bot.guilds:
                    try:
                        member = await guild.fetch_member(int(discord_user_id))
                        if member:
                            await member.edit(nick=roblox_name[:32], reason="Automated Roblox verification nickname sync.")
                    except Exception: pass

                user = await bot.fetch_user(int(discord_user_id))
                if user:
                    embed = discord.Embed(title="✅ Account Verification Complete!", description="Your server profile has been linked and your nickname synced to the official Roblox registry database.", color=discord.Color.green())
                    embed.add_field(name="Roblox Username", value=f"[{roblox_name}](https://www.roblox.com/users/{roblox_id}/profile)", inline=True)
                    embed.add_field(name="Account User ID", value=f"`{roblox_id}`", inline=True)
                    await user.send(embed=embed)
            except Exception: pass

        bot.loop.create_task(send_dm_and_rename())
        return render_template_string(SUCCESS_HTML_PAGE, username=roblox_name)

    except Exception as e:
        return f"❌ System callback operation loop encountered exception: {str(e)}", 500

def run_discord_bot():
    if not DISCORD_TOKEN: return
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
    except Exception as e:
        print(f"❌ Discord bot loop crashed: {e}")
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception: pass
        loop.close()

# ── Adaptive Deployment Environment Detection (Prevents Port Collisions) ───────
if __name__ != "__main__":
    print("🛰️ WSGI/Gunicorn environment detected. Launching background bot worker...")
    threading.Thread(target=run_discord_bot, daemon=True).start()
else:
    print("🛰️ Direct script execution detected. Initializing Flask development engine...")
    threading.Thread(target=run_discord_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
