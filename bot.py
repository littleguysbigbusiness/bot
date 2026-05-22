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
import secrets
import urllib.parse
from flask import Flask, request, redirect, session
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── Flask Initialization ────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Sheet-backed Pending Verifications ────────────────────────────────────────
# Uses the TempStates sheet (columns: A=token, B=discord_id) instead of
# an in-memory dict, so Render restarts don't wipe pending sessions.

VERIFIED_USERS_SHEET = "VerifiedUsers"
TEMP_STATES_SHEET    = "TempStates"

def temp_states_add(token: str, discord_id: str):
    """Write a pending token -> discord_id pair to TempStates sheet with expiry timestamp."""
    expiry = (datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        requests.post(TEMP_STATES_APPEND_URL, headers=sheets_headers(), json={"values": [[token, discord_id, expiry]]}, timeout=10)
    except Exception as e:
        print(f"[TempStates] Write error: {e}")

def temp_states_pop(token: str):
    """Look up, validate expiry, and delete a token from TempStates.
    Returns discord_id string, "EXPIRED" if token timed out, or None if not found."""
    try:
        resp = requests.get(TEMP_STATES_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
    except Exception as e:
        print(f"[TempStates] Read error: {e}")
        return None

    for i, row in enumerate(rows):
        if len(row) >= 2 and row[0].strip() == token.strip():
            discord_id = row[1].strip()
            expiry_str = row[2].strip() if len(row) >= 3 else None

            # Always clear the row first (one-time use)
            sheet_row = i + 1
            range_str = f"{TEMP_STATES_SHEET}!A{sheet_row}:C{sheet_row}"
            clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}:clear"
            try:
                requests.post(clear_url, headers=sheets_headers(), timeout=10)
            except Exception as e:
                print(f"[TempStates] Clear error: {e}")

            # Check expiry
            if expiry_str:
                try:
                    expiry_dt = datetime.datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M:%S")
                    if datetime.datetime.utcnow() > expiry_dt:
                        print(f"[TempStates] Token expired for discord_id {discord_id}")
                        return "EXPIRED"
                except Exception:
                    pass

            return discord_id
    return None

def temp_states_purge_expired():
    """Remove all expired rows from TempStates sheet."""
    try:
        resp = requests.get(TEMP_STATES_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        now = datetime.datetime.utcnow()
        for i, row in enumerate(rows):
            if len(row) >= 3:
                try:
                    expiry_dt = datetime.datetime.strptime(row[2].strip(), "%Y-%m-%dT%H:%M:%S")
                    if now > expiry_dt:
                        sheet_row = i + 1
                        range_str = f"{TEMP_STATES_SHEET}!A{sheet_row}:C{sheet_row}"
                        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}:clear"
                        requests.post(url, headers=sheets_headers(), timeout=10)
                        print(f"[TempStates] Purged expired row {sheet_row}")
                except Exception:
                    pass
    except Exception as e:
        print(f"[TempStates] Purge error: {e}")

def verified_users_log(discord_id: str, roblox_user_id: str, roblox_username: str):
    """Append or update a verified user record in VerifiedUsers sheet."""
    try:
        # Check if already exists and update
        resp = requests.get(VERIFIED_USERS_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if len(row) >= 1 and row[0].strip() == str(discord_id).strip():
                sheet_row = i + 1
                range_str = f"{VERIFIED_USERS_SHEET}!A{sheet_row}:C{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"
                requests.put(url, headers=sheets_headers(), json={"values": [[discord_id, roblox_user_id, roblox_username]]}, timeout=10)
                print(f"[VerifiedUsers] Updated existing record for Discord ID {discord_id}")
                return
        # Not found, append new row
        requests.post(VERIFIED_USERS_APPEND_URL, headers=sheets_headers(), json={"values": [[discord_id, roblox_user_id, roblox_username]]}, timeout=10)
        print(f"[VerifiedUsers] Logged new verified user: {discord_id} -> {roblox_username}")
    except Exception as e:
        print(f"[VerifiedUsers] Log error: {e}")

def is_already_verified(user_id: int) -> bool:
    """Check VerifiedUsers sheet for an existing Discord ID entry."""
    try:
        resp = requests.get(VERIFIED_USERS_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for row in rows:
            if len(row) >= 1 and row[0].strip() == str(user_id).strip():
                return True
    except Exception as e:
        print(f"[VerifiedUsers] Check error: {e}")
    return False

# ── Flask Routes ───────────────────────────────────────────────────────────────
@app.route('/')
def home():
    return "BWR7 Warnings Bot is Online Framework Stable!", 200

@app.route('/callback', methods=['GET'])
def callback():
    code = request.args.get('code')
    state_token = request.args.get('state')

    print(f"[Callback] code={'YES' if code else 'MISSING'}, state_token={'YES' if state_token else 'MISSING'}")

    if not code:
        print("[Callback] FAILED: No code received")
        return "Error: No code received.", 400
    if not state_token:
        print("[Callback] FAILED: No state token")
        return "Error: Missing state parameter.", 400

    # Resolve and consume the state token -> discord_id
    discord_id = temp_states_pop(state_token)
    if discord_id == "EXPIRED":
        print(f"[Callback] FAILED: Token expired")
        return (
            "⛔ Verification session expired. Your link is only valid for 5 minutes. "
            "Please run /verify again in Discord to get a fresh link. "
            "If you believe this is an error, contact a server administrator.", 400
        )
    if not discord_id:
        print(f"[Callback] FAILED: Token not found — possible bot/replay attack")
        return (
            "⛔ Invalid verification session. This link has already been used or does not exist. "
            "If you are trying to verify, please run /verify in Discord. "
            "Repeated invalid attempts may be flagged as suspicious activity.", 400
        )

    # Token exchange
    try:
        token_resp = requests.post(
            "https://apis.roblox.com/oauth/v1/token",
            data={
                "client_id": os.environ.get("ROBLOX_CLIENT_ID"),
                "client_secret": os.environ.get("ROBLOX_CLIENT_SECRET"),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://bot-h57e.onrender.com/callback"
            },
            timeout=10
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except requests.RequestException as e:
        print(f"Token exchange failed: {e}")
        return "Error: Token exchange failed.", 502

    access_token = token_data.get("access_token")
    if not access_token:
        print(f"No access token in response: {token_data}")
        return "Error: Authorization failed.", 400

    # Get user info
    try:
        user_resp = requests.get(
            "https://apis.roblox.com/oauth/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        user_resp.raise_for_status()
        user_info = user_resp.json()
    except requests.RequestException as e:
        print(f"User info fetch failed: {e}")
        return "Error: Could not fetch user info.", 502

    roblox_name = user_info.get("preferred_username")
    roblox_sub  = user_info.get("sub", "")  # Roblox user ID (stable, never changes)
    if not roblox_name:
        return "Error: Could not retrieve Roblox username.", 400

    # Thread-safe scheduling from Flask's sync thread
    future = asyncio.run_coroutine_threadsafe(
        update_discord_member(discord_id, roblox_sub, roblox_name),
        bot.loop
    )
    try:
        future.result(timeout=10)
    except Exception as e:
        print(f"Discord update failed: {e}")
        return "Error: Could not update Discord member.", 500

    return "Verification successful! You can close this window.", 200

@app.route('/privacy')
def privacy():
    return "Privacy Policy: We use your Roblox username solely to update your Discord nickname for community identification purposes. No other personal data is collected or stored.", 200

@app.route('/terms')
def terms():
    return "Terms of Service: By verifying your account, you consent to the bot updating your Discord server nickname to match your Roblox username.", 200

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1476812926521184276
STATIC_STATUS_ID  = 1505808587807789117
APPEAL_CHANNEL_ID = 1505891264032149574

GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"

TEMP_STATES_READ_URL   = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{TEMP_STATES_SHEET}!A:C"
TEMP_STATES_APPEND_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{TEMP_STATES_SHEET}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

VERIFIED_USERS_READ_URL   = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFIED_USERS_SHEET}!A:C"
VERIFIED_USERS_APPEND_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFIED_USERS_SHEET}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

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

PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He's A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff",
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger",
    "Tickets v2", "BD Department", "BM Department"
]

# ── Google Authentication ──────────────────────────────────────────────────────
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

# ── Status Page Embedding Logic ────────────────────────────────────────────────
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
        if not temp_states_cleanup.is_running():
            temp_states_cleanup.start()

bot = WarningsBot()

# ── Helpers ────────────────────────────────────────────────────────────────────
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

async def update_discord_member(discord_id, roblox_user_id, roblox_username):
    """Update Discord nickname and log to VerifiedUsers sheet."""
    guild = bot.guilds[0]
    try:
        member = await guild.fetch_member(int(discord_id))
        await member.edit(nick=roblox_username)
        print(f"[Verify] Updated nickname for {discord_id} -> {roblox_username}")
    except Exception as e:
        print(f"[Verify] Failed to update nickname: {e}")
    # Log to sheet regardless of nickname success
    verified_users_log(discord_id, roblox_user_id, roblox_username)

# ── Background Tasks ───────────────────────────────────────────────────────────
@tasks.loop(seconds=60)
async def update_status_embed():
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if not channel:
            return
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

@tasks.loop(minutes=5)
async def temp_states_cleanup():
    """Purge expired TempStates rows every 5 minutes."""
    await bot.wait_until_ready()
    temp_states_purge_expired()

@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    print("[Sweeper] Starting automated infraction expiration analysis...")
    rows = read_all_rows()
    if not rows:
        return

    current_date = datetime.datetime.utcnow().date()

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
        except Exception:
            continue

        if current_date >= expiry_date:
            print(f"[Sweeper] Processing automatic termination for User ID: {user_id_str} [{restriction_type}]")
            for guild in bot.guilds:
                if restriction_type == "Ban":
                    try:
                        await guild.unban(discord.Object(id=int(user_id_str)), reason="System Auto-Expiry: Temporal duration limit exceeded.")
                        print(f"[Sweeper] Successfully unbanned ID {user_id_str}.")
                    except Exception as e:
                        print(f"[Sweeper] Failed to auto-unban {user_id_str}: {e}")
                elif restriction_type == "Timeout":
                    try:
                        member = await guild.fetch_member(int(user_id_str))
                        if member:
                            await member.timeout(None, reason="System Auto-Expiry: Temporal duration limit exceeded.")
                            print(f"[Sweeper] Successfully unmuted ID {user_id_str}.")
                    except Exception:
                        pass

            row[COL_REVOKED]    = "TRUE"
            row[COL_REVOKED_BY] = "System Auto-Expiry"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx + 2, row)
            await asyncio.sleep(1)

# ── Appeal System ──────────────────────────────────────────────────────────────
class AppealReasonModal(discord.ui.Modal, title="Submit Case File Appeal"):
    appeal_reason = discord.ui.TextInput(
        label="Why should this infraction be removed?",
        style=discord.TextStyle.paragraph,
        placeholder="Provide context or evidence...",
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
            await interaction.followup.send("❌ Error: Appeal review channel missing.", ephemeral=True)
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
        await interaction.followup.send("✅ **Success!** Your system appeal file has been dispatched.", ephemeral=True)

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
            await target_user.send(embed=discord.Embed(
                title="✅ Appeal Approved",
                description=f"Your appeal for Case ID `{case_id}` has been accepted. The infraction has been lifted.",
                color=discord.Color.green()
            ))
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
            await target_user.send(embed=discord.Embed(
                title="❌ Appeal Denied",
                description=f"Your appeal for Case ID `{case_id}` has been rejected.",
                color=discord.Color.red()
            ))
        except Exception:
            pass

        embed.color = discord.Color.red()
        embed.title = "❌ Appeal Evaluated & Rejected"
        embed.set_footer(text=f"Rejected upon review by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

# ── Universal Punishment Lifter ────────────────────────────────────────────────
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
                        if role.name.strip() in PROTECTED_ROLE_NAMES:
                            continue
                        try:
                            await member.add_roles(role)
                            restored += 1
                        except Exception:
                            pass
                return f"Staff Suspension lifted ({restored} roles restored)"
        except Exception as e:
            return f"Staff Suspension role error: {e}"

    return "Database trail flagged"

# ── Master Mod Action Engine ───────────────────────────────────────────────────
async def run_moderation_action(
    interaction: discord.Interaction,
    target_id: str,
    target_name: str,
    target_member: discord.Member,
    reason: str,
    restriction_type: str,
    source: str,
    end_date: str,
    timeout_duration: datetime.timedelta = None
):
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
                    execution_notes = "Timed out natively via duration utility"
                except Exception as e:
                    execution_notes = f"Logged (Timeout failed: {e})"
            else:
                try:
                    await target_member.timeout(datetime.timedelta(days=1), reason=reason)
                    execution_notes = "Timed out for 24 Hours (Default)"
                    final_expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception as e:
                    execution_notes = f"Logged (Timeout failed: {e})"

        elif restriction_type == "Ban":
            try:
                await interaction.guild.ban(discord.Object(id=int(target_id)), delete_message_days=1, reason=reason)
                execution_notes = "Banned cleanly via user Object ID lookup"
            except Exception as e:
                execution_notes = f"Logged (API Ban execution failed: {e})"

    append_row([
        target_id, target_name, str(interaction.user), str(interaction.user.id),
        reason, timestamp, warning_id, "FALSE", "", "", source, restriction_type,
        start_date, final_expiry, warning_id
    ])

    embed = discord.Embed(
        title=f"🛑 User Log Added ({restriction_type})",
        description=f"A formal {restriction_type.lower()} record has been generated and securely logged to the central database.",
        color=discord.Color.from_rgb(44, 62, 80)
    )
    if target_member:
        embed.set_thumbnail(url=target_member.display_avatar.url)
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

# ── Display Layout Engine ──────────────────────────────────────────────────────
def build_historical_log_embed(title_text: str, warnings_list: list, thumbnail_url: str = None) -> discord.Embed:
    embed = discord.Embed(title=title_text, color=discord.Color.from_rgb(44, 62, 80))
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    active_txt = ""
    revoked_txt = ""

    for r, _ in warnings_list:
        case_id  = r[COL_INCIDENT_ID].strip()
        rest_type = r[COL_RESTRICTION].strip()
        context  = r[COL_SOURCE].strip()
        reason   = r[COL_REASON].strip()
        issued   = r[COL_TIMESTAMP][:10]
        expires  = r[COL_END_DATE].strip()
        is_revoked = r[COL_REVOKED].strip().upper() == "TRUE"

        log_block = f"▪ Case ID: {case_id} | Type: {rest_type} | Context: {context}\n  Reason: {reason}\n  Issued: {issued} | Expires: {expires}\n"

        if is_revoked:
            revoked_by = r[COL_REVOKED_BY].strip()
            log_block += f"  ❌ REVOKED BY: {revoked_by}\n\n"
            revoked_txt += log_block
        else:
            log_block += "\n"
            active_txt += log_block

    if not active_txt:
        active_txt = "No active infractions registered against this profile.\n"
    if not revoked_txt:
        revoked_txt = "No historical logs have been revoked or cleared.\n"

    embed.add_field(name="⚠️ Active Infractions & Restrictions", value=f"```text\n{active_txt.strip()}\n```", inline=False)
    embed.add_field(name="✅ Historical Archive (Revoked/Cleared Logs)", value=f"```text\n{revoked_txt.strip()}\n```", inline=False)
    embed.timestamp = datetime.datetime.utcnow()
    return embed

# ── Slash Commands ─────────────────────────────────────────────────────────────
BANNED_PREFIXES = ["CEO", "VCEO"]

def build_verify_url(discord_id: str) -> str:
    """Generate a fresh OAuth URL and store the state token."""
    state_token = secrets.token_urlsafe(32)
    temp_states_add(state_token, discord_id)
    params = urllib.parse.urlencode({
        "client_id": os.environ.get("ROBLOX_CLIENT_ID"),
        "response_type": "code",
        "redirect_uri": "https://bot-h57e.onrender.com/callback",
        "scope": "openid profile",
        "state": state_token
    })
    return f"https://apis.roblox.com/oauth/v1/authorize?{params}"

@bot.tree.command(name="verify", description="Link your Roblox account to Discord")
async def verify(interaction: discord.Interaction):
    if is_already_verified(interaction.user.id):
        await interaction.response.send_message(
            "✅ You are already verified! Use `/reverify` to re-link your account.", ephemeral=True
        )
        return

    auth_url = build_verify_url(str(interaction.user.id))
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Login with Roblox", url=auth_url))
    await interaction.response.send_message(
        "🔗 Click below to link your Roblox account.\n"
        "⚠️ This link is **personal** — do not share it.\n"
        "⏱️ The link expires in **5 minutes**.",
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="reverify", description="Re-link your Roblox account to Discord")
async def reverify(interaction: discord.Interaction):
    auth_url = build_verify_url(str(interaction.user.id))
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Re-link with Roblox", url=auth_url))
    await interaction.response.send_message(
        "🔗 Click below to re-link your Roblox account.\n"
        "⚠️ This link is **personal** — do not share it.\n"
        "⏱️ The link expires in **5 minutes**.",
        view=view,
        ephemeral=True
    )

@bot.tree.command(name="setprefix", description="Set a custom prefix for your nickname (e.g. Driver-YourName)")
@app_commands.describe(prefix="Your desired prefix (e.g. Driver, Trainer). CEO and VCEO are not allowed.")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)

    # Check banned prefixes (case-insensitive)
    prefix_upper = prefix.strip().upper()
    for banned in BANNED_PREFIXES:
        if prefix_upper == banned or prefix_upper.startswith(banned):
            await interaction.followup.send(
                f"❌ The prefix `{prefix}` is not allowed. `CEO` and `VCEO` are reserved prefixes.",
                ephemeral=True
            )
            return

    # Must be verified first
    if not is_already_verified(interaction.user.id):
        await interaction.followup.send(
            "❌ You must be verified first. Run `/verify` to link your Roblox account.",
            ephemeral=True
        )
        return

    # Get their Roblox username from VerifiedUsers sheet
    try:
        resp = requests.get(VERIFIED_USERS_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        roblox_username = None
        for row in rows:
            if len(row) >= 3 and row[0].strip() == str(interaction.user.id):
                roblox_username = row[2].strip()
                break
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch your Roblox username: {e}", ephemeral=True)
        return

    if not roblox_username:
        await interaction.followup.send(
            "❌ Could not find your Roblox username on record. Try `/reverify`.",
            ephemeral=True
        )
        return

    new_nick = f"{prefix.strip()}-{roblox_username}"

    # Discord nickname max length is 32 chars
    if len(new_nick) > 32:
        await interaction.followup.send(
            f"❌ The resulting nickname `{new_nick}` is too long ({len(new_nick)}/32 chars). Use a shorter prefix.",
            ephemeral=True
        )
        return

    try:
        member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
        await member.edit(nick=new_nick)
        await interaction.followup.send(f"✅ Your nickname has been updated to `{new_nick}`.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I don't have permission to change your nickname.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to update nickname: {e}", ephemeral=True)

@bot.tree.command(name="send_message", description="[Admin] Dispatch an announcement or embed JSON into a channel")
@app_commands.describe(channel_id="The numerical unique ID of your target channel", message="Optional text message content", embed_json="Optional embed structured in valid JSON")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    cleaned_chan_id = extract_id(channel_id)
    target_channel = bot.get_channel(int(cleaned_chan_id))

    if not target_channel:
        return await interaction.followup.send(f"❌ **Error:** Target channel ID `{cleaned_chan_id}` could not be located.")

    if not message and not embed_json:
        return await interaction.followup.send("❌ **Error:** You must provide either a `message` or `embed_json`.")

    target_embed = None
    if embed_json:
        try:
            clean_json = embed_json.strip()
            if clean_json.startswith("```json"): clean_json = clean_json[7:]
            elif clean_json.startswith("```"): clean_json = clean_json[3:]
            if clean_json.endswith("```"): clean_json = clean_json[:-3]

            data = json.loads(clean_json.strip())
            embed_dict = data["embeds"][0] if "embeds" in data and data["embeds"] else data
            target_embed = discord.Embed.from_dict(embed_dict)
        except Exception as e:
            return await interaction.followup.send(f"❌ **JSON Formatting Error:**\n```text\n{e}\n```")

    try:
        await target_channel.send(content=message, embed=target_embed)
        await interaction.followup.send(f"✅ **Success!** Message transmitted to <#{cleaned_chan_id}>.")
    except discord.Forbidden:
        await interaction.followup.send(f"❌ **API Error:** Missing permissions to write inside <#{cleaned_chan_id}>.")
    except Exception as e:
        await interaction.followup.send(f"❌ **Transmission Error:**\n```text\n{e}\n```")

@bot.tree.command(name="revokeaction", description="[Admin] Revoke an active moderation file and lift its punishment")
@app_commands.describe(case_id="The Case ID to revoke (e.g. AB12CD34)")
async def revokeaction(interaction: discord.Interaction, case_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    row, sheet_row = find_warning_by_id(case_id)
    if row is None:
        return await interaction.followup.send(f"❌ Case ID `{case_id}` not found.")
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

@bot.tree.command(name="modstats", description="[Admin] View server-wide moderation metrics")
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
        rest_type  = row[COL_RESTRICTION].strip()
        uid        = row[COL_USER_ID].strip()
        username   = row[COL_USERNAME].strip()
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

@bot.tree.command(name="appeal", description="Submit an appeal for an active infraction file")
async def appeal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    active_cases = [(r, idx) for r, idx in warnings if pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not active_cases:
        return await interaction.followup.send("✅ You have no active warnings or restrictions to appeal!", ephemeral=True)
    await interaction.followup.send("📋 **Infraction System Appeal Port:**\nSelect the case file from the dropdown:", view=AppealDropdownView(active_cases), ephemeral=True)

@bot.tree.command(name="viewmywarnings", description="View all your warnings (private)")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    warnings = get_user_warnings(str(interaction.user.id))
    embed = build_historical_log_embed("👤 Your Personal Compliance History", warnings, interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View warnings for any user")
@app_commands.describe(user_target="The ID or @mention of the target user")
async def viewwarnings(interaction: discord.Interaction, user_target: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()

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
        except Exception:
            pass

    embed = build_historical_log_embed(f"📋 Historical Audit File: {cleaned_id}", warnings, thumb)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="warn", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="issuewarning", description="[Admin] Issue a warning to a user")
@app_commands.describe(user="The user member profile", reason="Reason for the warning", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def issuewarning(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="timeout", description="[Admin] Time out a user and log to sheet")
@app_commands.describe(user="The user member profile", reason="Reason for timeout", duration_amount="The number value for length", duration_unit="The unit of measurement", source="Platform context")
@app_commands.choices(
    source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")],
    duration_unit=[app_commands.Choice(name="Minutes", value="minutes"), app_commands.Choice(name="Hours", value="hours"), app_commands.Choice(name="Days", value="days")]
)
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, duration_amount: int, duration_unit: app_commands.Choice[str], source: app_commands.Choice[str] = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    if duration_amount <= 0:
        return await interaction.followup.send("❌ **Error:** Duration must be a positive integer.")

    unit = duration_unit.value
    if unit == "minutes":   delta = datetime.timedelta(minutes=duration_amount)
    elif unit == "hours":   delta = datetime.timedelta(hours=duration_amount)
    else:                   delta = datetime.timedelta(days=duration_amount)

    final_expiry_stamp = (datetime.datetime.utcnow() + delta).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", source.value if source else "Discord", final_expiry_stamp, timeout_duration=delta)

@bot.tree.command(name="ban", description="[Admin] Ban a user from the server")
@app_commands.describe(user_target="The ID or mention of the user to ban", reason="Reason for ban", source="Platform context", end_date="Optional expiry date (YYYY-MM-DD)")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def ban_cmd(interaction: discord.Interaction, user_target: str, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
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
        except Exception:
            pass

    clean_end_date = end_date.strip() if end_date else "Never"
    await run_moderation_action(interaction, cleaned_id, target_name, target_member, reason, "Ban", source.value if source else "Discord", clean_end_date)

@bot.tree.command(name="staff_suspension", description="[Admin] Suspend a staff member and strip non-protected roles")
@app_commands.describe(user="The staff member profile", reason="Reason for suspension", source="Platform context", end_date="Expiry date (YYYY-MM-DD) REQUIRED")
@app_commands.choices(source=[app_commands.Choice(name="Discord Server", value="Discord"), app_commands.Choice(name="Roblox In-Game", value="Roblox Game")])
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str, source: app_commands.Choice[str] = None):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    try:
        datetime.datetime.strptime(end_date.strip(), "%Y-%m-%d")
    except ValueError:
        return await interaction.followup.send("❌ **Error:** Expiry date format required: `YYYY-MM-DD`")

    role_ids = [r.id for r in user.roles if r.name != "@everyone" and not r.managed]
    save_suspended_roles(user.id, role_ids)
    for role in user.roles:
        if role.name != "@everyone" and not role.managed:
            if role.name.strip() in PROTECTED_ROLE_NAMES:
                continue
            try:
                await user.remove_roles(role)
            except Exception:
                pass
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Staff Suspension", source.value if source else "Discord", end_date)

@bot.tree.command(name="restoreroles", description="Restore your original roles if your suspension has expired")
async def restoreroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    history = get_user_warnings(str(user_id))
    suspensions = [r for r, _ in history if pad(r)[COL_RESTRICTION].strip() == "Staff Suspension" and pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not suspensions:
        return await interaction.followup.send("❌ No active Staff Suspension logs.", ephemeral=True)

    active_suspension = pad(suspensions[-1])
    expiry_str = active_suspension[COL_END_DATE].strip()
    if expiry_str == "Never":
        return await interaction.followup.send("🔒 Suspension is marked as Permanent.", ephemeral=True)

    try:
        if datetime.datetime.strptime(expiry_str, "%Y-%m-%d") > datetime.datetime.utcnow():
            return await interaction.followup.send(f"⏳ Suspension active until `{expiry_str}`.", ephemeral=True)
    except ValueError:
        return await interaction.followup.send("❌ Format corrupted on sheet.", ephemeral=True)

    saved_ids = pop_suspended_roles(user_id)
    if not saved_ids:
        return await interaction.followup.send("⚠️ Backup file missing.", ephemeral=True)

    restored = 0
    for r_id in saved_ids:
        role = interaction.guild.get_role(r_id)
        if role:
            if role.name.strip() in PROTECTED_ROLE_NAMES:
                continue
            try:
                await interaction.user.add_roles(role)
                restored += 1
            except Exception:
                pass

    for r, idx in history:
        if pad(r)[COL_INCIDENT_ID] == active_suspension[COL_INCIDENT_ID]:
            r_padded = pad(r)
            r_padded[COL_REVOKED]    = "TRUE"
            r_padded[COL_REVOKED_BY] = "System Auto-Restore"
            r_padded[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx, r_padded)
            break

    await interaction.followup.send(f"✅ Restored **{restored}** staff roles cleanly.", ephemeral=True)

# ── Bot Runner ─────────────────────────────────────────────────────────────────
def run_discord_bot():
    if not DISCORD_TOKEN:
        return
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
        except Exception:
            pass
        loop.close()

# ── Deployment Entry Point ─────────────────────────────────────────────────────
if __name__ != "__main__":
    print("🛰️ WSGI/Gunicorn environment detected. Launching background bot worker...")
    threading.Thread(target=run_discord_bot, daemon=True).start()
else:
    print("🛰️ Direct script execution detected. Initializing Flask development engine...")
    threading.Thread(target=run_discord_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
