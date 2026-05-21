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
import time
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

ROBLOX_CLIENT_ID     = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI  = "https://bot-h57e.onrender.com/roblox_callback" 
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

SHEET_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
SHEET_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
SHEET_UPDATE_BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
VERIFY_READ_URL   = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET_NAME}!A:C"
VERIFY_APPEND_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
STATE_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET_NAME}!A:C"
STATE_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

ROLES_BACKUP_FILE = "suspended_roles.json"
COL_USER_ID, COL_USERNAME, COL_ISSUED_BY, COL_ISSUED_ID, COL_REASON, COL_TIMESTAMP, COL_INCIDENT_ID, COL_REVOKED, COL_REVOKED_BY, COL_REVOKED_AT, COL_SOURCE, COL_RESTRICTION, COL_START_DATE, COL_END_DATE, COL_ALT_INC_ID = range(15)

PROTECTED_ROLE_NAMES = ["Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS", "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key", "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2", "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", "Tickets v2", "BD Department", "BM Department"]

# ── Google Authentication ──────────────────────────────────────────────────────
from google.oauth2 import service_account
from google.auth.transport.requests import Request
creds = None
SECRET_FILE_PATH = "/etc/secrets/service_account.json"
ALTERNATIVE_PATH = "service_account.json"
TARGET_PATH = SECRET_FILE_PATH if os.path.exists(SECRET_FILE_PATH) else ALTERNATIVE_PATH

if os.path.exists(TARGET_PATH):
    try:
        creds = service_account.Credentials.from_service_account_file(TARGET_PATH, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        print(f"✅ Google Sheets engine connected.")
    except Exception as e:
        print(f"❌ Credentials parsing issue: {e}")

def sheets_headers():
    global creds
    if creds:
        if not creds.valid:
            creds.refresh(Request())
        token = creds.token
    else:
        token = ""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── Database Helpers ───────────────────────────────────────────────────────────
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
    except: return []

def append_row(row):
    try: requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
    except: pass

def update_row(row_index, sheet, row):
    try:
        range_str = f"{sheet}!A{row_index}:O{row_index}" if sheet == SHEET_NAME else f"{sheet}!A{row_index}:C{row_index}"
        url = f"{SHEET_UPDATE_BASE}{range_str}?valueInputOption=RAW"
        requests.put(url, headers=sheets_headers(), json={"values": [row]}, timeout=10)
    except: pass

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

# ── Roblox Verification State Helpers ──────────────────────────────────────────
def get_verified_roblox_id(discord_id: str) -> str:
    try:
        resp = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for row in rows[1:]:
            if row and row[0].strip() == str(discord_id).strip():
                return row[1].strip()
    except: pass
    return None

def save_oauth_state_to_cloud(state_token: str, discord_user_id: int):
    try:
        ts = str(int(time.time()))
        payload = {"values": [[str(state_token), str(discord_user_id), ts]]}
        requests.post(STATE_APPEND_URL, headers=sheets_headers(), json=payload, timeout=10)
    except Exception as e: print(f"[Cloud State Error] {e}")

def pop_oauth_state_from_cloud(state_token: str) -> str:
    for attempt in range(5):
        try:
            resp = requests.get(STATE_READ_URL, headers=sheets_headers(), timeout=10)
            rows = resp.json().get("values", [])
            if rows and len(rows) > 1:
                for i, row in enumerate(rows[1:]):
                    if row and row[0].strip() == str(state_token).strip():
                        discord_id = row[1].strip()
                        try:
                            if (int(time.time()) - int(row[2].strip())) > 300: return None
                        except: pass
                        update_row(i + 2, STATE_SHEET_NAME, ["", "", ""])
                        return discord_id
            time.sleep(1)
        except: pass
    return None

# ── Roles Backup Helpers ───────────────────────────────────────────────────────
def save_suspended_roles(user_id, role_ids):
    data = {}
    if os.path.exists(ROLES_BACKUP_FILE):
        try:
            with open(ROLES_BACKUP_FILE, "r") as f: data = json.load(f)
        except: data = {}
    data[str(user_id)] = role_ids
    with open(ROLES_BACKUP_FILE, "w") as f: json.dump(data, f)

def pop_suspended_roles(user_id):
    if not os.path.exists(ROLES_BACKUP_FILE): return []
    try:
        with open(ROLES_BACKUP_FILE, "r") as f: data = json.load(f)
        role_ids = data.pop(str(user_id), [])
        with open(ROLES_BACKUP_FILE, "w") as f: json.dump(data, f)
        return role_ids
    except: return []

# ── Status Page Embed ─────────────────────────────────────────────────────────
STATUS_EMOJI = {"operational": "✅", "degraded_performance": "🟨", "partial_outage": "🟧", "major_outage": "🔴", "under_maintenance": "🔵", "unknown": "⬜"}
STATUS_COLOR = {"none": discord.Color.green(), "minor": discord.Color.yellow(), "major": discord.Color.red(), "critical": discord.Color.red(), "maintenance": discord.Color.blue(), "unknown": discord.Color.light_grey()}

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
        lines = [f"{STATUS_EMOJI.get(c.get('status','unknown'), '⬜')} **{c['name']}** — {c.get('status','unknown').replace('_', ' ').title()}" for c in visible]
        embed.add_field(name="🔧 Components", value="\n".join(lines), inline=False)
    if incidents:
        embed.add_field(name="⚠️ Active Incidents", value="\n".join([f"🚨 **[{inc.get('impact','?').upper()}]** {inc['name']}" for inc in incidents[:3]]), inline=False)
    else:
        embed.add_field(name="⚠️ Incidents", value="✅ No active incidents", inline=False)
    embed.set_footer(text="🔄 Updates every 60 seconds")
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
        if not update_status_embed.is_running(): update_status_embed.start()
        if not automatic_expiry_sweeper.is_running(): automatic_expiry_sweeper.start()

bot = WarningsBot()

def is_admin(interaction: discord.Interaction):
    m = interaction.user
    return isinstance(m, discord.Member) and (m.guild_permissions.administrator or m.guild_permissions.manage_guild or m.guild_permissions.moderate_members)

async def dm_user(user, embed):
    try: await user.send(embed=embed); return True
    except: return False

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
            except: pass
    except: pass

@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    rows = read_all_rows()
    if not rows: return
    current_date = datetime.datetime.utcnow().date()

    for idx, raw_row in enumerate(rows):
        row = pad(raw_row)
        user_id_str = row[COL_USER_ID].strip()
        if row[COL_REVOKED].strip().upper() == "TRUE" or not user_id_str or row[COL_END_DATE].strip() in ("Never", "", "None"):
            continue

        try:
            expiry_date = datetime.datetime.strptime(row[COL_END_DATE].strip().split(" ")[0], "%Y-%m-%d").date()
        except: continue

        if current_date >= expiry_date:
            for guild in bot.guilds:
                if row[COL_RESTRICTION] == "Ban":
                    try: await guild.unban(discord.Object(id=int(user_id_str)))
                    except: pass
                elif row[COL_RESTRICTION] == "Timeout":
                    try:
                        member = await guild.fetch_member(int(user_id_str))
                        if member: await member.timeout(None)
                    except: pass

            row[COL_REVOKED] = "TRUE"
            row[COL_REVOKED_BY] = "System Auto-Expiry"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(idx + 2, SHEET_NAME, row)
            await asyncio.sleep(1)

# ── Appeal Views ─────────────────────────────────────────────────────────────
class AppealReasonModal(discord.ui.Modal, title="Submit Case File Appeal"):
    appeal_reason = discord.ui.TextInput(label="Why should this be removed?", style=discord.TextStyle.paragraph, required=True, max_length=500)
    def __init__(self, case_id, original_reason, restriction_type):
        super().__init__()
        self.case_id = case_id
        self.original_reason = original_reason
        self.restriction_type = restriction_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        review_channel = bot.get_channel(APPEAL_CHANNEL_ID)
        if not review_channel: return await interaction.followup.send("❌ Error: Channel missing.", ephemeral=True)

        embed = discord.Embed(title="📥 System Appeal Submitted", color=discord.Color.orange())
        embed.add_field(name="Appellant", value=f"{interaction.user.mention}\n`ID: {interaction.user.id}`")
        embed.add_field(name="Case ID", value=f"`{self.case_id}`")
        embed.add_field(name="Action Type", value=self.restriction_type)
        embed.add_field(name="Original Reason", value=f"```text\n{self.original_reason}\n```", inline=False)
        embed.add_field(name="Appellant Statement", value=f"```text\n{self.appeal_reason.value}\n```", inline=False)
        
        await review_channel.send(embed=embed, view=AppealReviewButtons())
        await interaction.followup.send("✅ Appeal dispatched.", ephemeral=True)

class AppealDropdownMenu(discord.ui.Select):
    def __init__(self, user_active_cases):
        options = [discord.SelectOption(label=f"Case: {r[COL_INCIDENT_ID].strip()} [{r[COL_RESTRICTION].strip()}]", value=r[COL_INCIDENT_ID].strip()) for r, _ in user_active_cases]
        super().__init__(placeholder="Select an active infraction...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        case_id = self.values[0]
        row, _ = find_warning_by_id(case_id)
        if not row: return await interaction.response.send_message("❌ Case resolved.", ephemeral=True)
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
        app_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        row, sheet_row = find_warning_by_id(case_id)
        if row and row[COL_REVOKED].strip().upper() != "TRUE":
            await execute_live_punishment_revocation(interaction.guild, row, str(interaction.user))
            row[COL_REVOKED] = "TRUE"
            row[COL_REVOKED_BY] = f"Appeal Appr: {interaction.user}"
            row[COL_REVOKED_AT] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            update_row(sheet_row, SHEET_NAME, row)

        try:
            u = await bot.fetch_user(app_id)
            await u.send(embed=discord.Embed(title="✅ Appeal Approved", description=f"Appeal for `{case_id}` accepted.", color=discord.Color.green()))
        except: pass

        embed.color = discord.Color.green()
        embed.title = "✅ Appeal Cleared & Approved"
        embed.set_footer(text=f"Approved by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(label="Deny Appeal", style=discord.ButtonStyle.danger, custom_id="deny_appeal_btn", emoji="🔴")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        await interaction.response.defer()
        embed = interaction.message.embeds[0]
        case_id = embed.fields[1].value.replace("`", "").strip()
        app_id = int(embed.fields[0].value.split("\n`ID: ")[1].replace("`", "").strip())

        try:
            u = await bot.fetch_user(app_id)
            await u.send(embed=discord.Embed(title="❌ Appeal Denied", description=f"Appeal for `{case_id}` rejected.", color=discord.Color.red()))
        except: pass

        embed.color = discord.Color.red()
        embed.title = "❌ Appeal Rejected"
        embed.set_footer(text=f"Rejected by {interaction.user}")
        await interaction.message.edit(embed=embed, view=None)

# ── Universal Moderation Action Engine ───────────────────────────────────────
async def execute_live_punishment_revocation(guild: discord.Guild, row, admin_name: str) -> str:
    uid = int(row[COL_USER_ID].strip())
    rest_type = row[COL_RESTRICTION].strip()
    if row[COL_SOURCE].strip() != "Discord": return "Logged (In-Game)"

    if rest_type == "Timeout":
        try:
            m = await guild.fetch_member(uid)
            if m: await m.timeout(None); return "Timeout lifted"
        except Exception as e: return f"Error: {e}"
    elif rest_type == "Ban":
        try: await guild.unban(discord.Object(id=uid)); return "Ban lifted"
        except Exception as e: return f"Error: {e}"
    elif rest_type == "Staff Suspension":
        ids = pop_suspended_roles(uid)
        if not ids: return "No roles backed up"
        try:
            m = await guild.fetch_member(uid)
            if m:
                c = 0
                for rid in ids:
                    role = guild.get_role(rid)
                    if role and role.name not in PROTECTED_ROLE_NAMES:
                        try: await m.add_roles(role); c += 1
                        except: pass
                return f"Restored {c} roles"
        except Exception as e: return f"Error: {e}"
    return "Flagged"

async def run_moderation_action(interaction: discord.Interaction, target_id: str, target_name: str, target_member: discord.Member, reason: str, restriction_type: str, source: str, end_date: str, timeout_duration: datetime.timedelta = None):
    wid = str(uuid.uuid4())[:8].upper()
    ts  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    start = ts[:10]
    expiry = end_date if end_date else "Never"
    notes = "Logged"
    dm_sent = False

    if target_member:
        dm = discord.Embed(title=f"⚠️ Notice: {restriction_type.upper()}", description=reason, color=discord.Color.dark_theme())
        dm.add_field(name="Case ID", value=wid)
        dm.add_field(name="Platform", value=source)
        dm.add_field(name="Expiry", value=expiry)
        dm.add_field(name="Appeal", value=f"[Open Form]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        dm_sent = await dm_user(target_member, dm)

    if source == "Discord" and target_member:
        if restriction_type == "Timeout":
            td = timeout_duration or datetime.timedelta(days=1)
            try:
                await target_member.timeout(td, reason=reason)
                notes = "Timed out"
                expiry = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception as e: notes = f"Timeout fail: {e}"
        elif restriction_type == "Ban":
            try: await interaction.guild.ban(target_member, reason=reason); notes = "Banned"
            except Exception as e: notes = f"Ban fail: {e}"

    append_row([target_id, target_name, str(interaction.user), str(interaction.user.id), reason, ts, wid, "FALSE", "", "", source, restriction_type, start, expiry, wid])

    embed = discord.Embed(title=f"🛑 Logged: {restriction_type}", description=reason, color=discord.Color.dark_theme())
    embed.add_field(name="Target", value=f"<@{target_id}>")
    embed.add_field(name="Case ID", value=wid)
    embed.add_field(name="Expiry", value=expiry)
    embed.set_footer(text=f"Revoke: /revokeaction {wid}")
    await interaction.followup.send(embed=embed)

def build_historical_log_embed(title: str, warnings: list, thumb: str = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.dark_theme())
    if thumb: embed.set_thumbnail(url=thumb)
    act, rev = "", ""
    for r, _ in warnings:
        block = f"▪ Case {r[COL_INCIDENT_ID].strip()} | {r[COL_RESTRICTION].strip()} | {r[COL_SOURCE].strip()}\n  Reason: {r[COL_REASON].strip()}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE].strip()}\n"
        if r[COL_REVOKED].strip().upper() == "TRUE": rev += block + f"  ❌ REVOKED BY: {r[COL_REVOKED_BY].strip()}\n\n"
        else: act += block + "\n"
    embed.add_field(name="⚠️ Active", value=f"```text\n{act.strip() or 'None'}\n```", inline=False)
    embed.add_field(name="✅ Archive", value=f"```text\n{rev.strip() or 'None'}\n```", inline=False)
    return embed

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.command(name="setprefix", description="Modify your server nickname with a prefix")
@app_commands.describe(prefix="The prefix to add to your name (Max 5 letters. CEO/VCEO restricted)")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)
    clean = prefix.strip()
    
    # 🛑 1. Block completely blank prefixes
    if not clean:
        return await interaction.followup.send("❌ **Error:** You cannot leave the prefix blank.", ephemeral=True)
        
    if len(clean) > 5: return await interaction.followup.send("❌ Max 5 characters.", ephemeral=True)
    if re.sub(r'[^A-Za-z0-9]', '', clean).upper() in ("CEO", "VCEO"): return await interaction.followup.send("❌ Restricted prefix.", ephemeral=True)
    
    # 🛑 2. Cleanly remove old prefix without breaking names that naturally have a hyphen
    base = interaction.user.display_name
    if " - " in base:
        base = base.split(" - ", 1)[-1].strip()
        
    new = f"{clean} - {base}"
    try:
        await interaction.user.edit(nick=new[:32])
        await interaction.followup.send(f"✅ Nickname updated to `{new[:32]}`", ephemeral=True)
    except Exception as e: 
        await interaction.followup.send(f"❌ Discord blocked the rename. Ensure bot's role is higher than yours. ({e})", ephemeral=True)

@bot.tree.command(name="verify", description="Link your Roblox account securely")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if get_verified_roblox_id(interaction.user.id): return await interaction.followup.send("⚠️ Account already linked.", ephemeral=True)
    token = str(uuid.uuid4())
    save_oauth_state_to_cloud(token, interaction.user.id)
    url = f"https://apis.roblox.com/oauth/v1/authorize?client_id={ROBLOX_CLIENT_ID}&redirect_uri={ROBLOX_REDIRECT_URI}&scope=openid+profile&response_type=code&state={token}"
    await interaction.followup.send(f"🔗 **[Click Here to Verify Account]({url})**", ephemeral=True)

@bot.tree.command(name="checklink", description="View your linked Roblox account")
async def checklink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
        for r in rows[1:]:
            if r and r[0].strip() == str(interaction.user.id):
                return await interaction.followup.send(f"✅ **Linked Account:** Roblox Name: `{r[2]}` (ID: `{r[1]}`)", ephemeral=True)
    except: pass
    await interaction.followup.send("❌ **Not Linked:** No Roblox profile bound.", ephemeral=True)

@bot.tree.command(name="unlink", description="Remove your Roblox account link")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
        for i, r in enumerate(rows[1:]):
            if r and r[0].strip() == str(interaction.user.id):
                update_row(i + 2, VERIFY_SHEET_NAME, ["", "", ""])
                return await interaction.followup.send("✅ **Success:** Link removed.", ephemeral=True)
    except: pass
    await interaction.followup.send("❌ **Error:** No link found.", ephemeral=True)

@bot.tree.command(name="forceunlink", description="[Admin] Manually remove a user's link")
async def forceunlink(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
        for i, r in enumerate(rows[1:]):
            if r and r[0].strip() == str(user.id):
                update_row(i + 2, VERIFY_SHEET_NAME, ["", "", ""])
                return await interaction.followup.send(f"✅ **Success:** Removed link for {user.mention}.", ephemeral=True)
    except: pass
    await interaction.followup.send("❌ **Error:** Not linked.", ephemeral=True)

@bot.tree.command(name="send_message", description="[Admin] Send Embed/Message")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    tc = bot.get_channel(int(extract_id(channel_id)))
    if not tc: return await interaction.followup.send("❌ Invalid Channel.")
    emb = None
    if embed_json:
        try: emb = discord.Embed.from_dict(json.loads(embed_json.strip("`").removeprefix("json")))
        except: return await interaction.followup.send("❌ Invalid JSON.")
    try: await tc.send(content=message, embed=emb); await interaction.followup.send("✅ Sent.")
    except Exception as e: await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="revokeaction", description="[Admin] Revoke moderation file")
async def revokeaction(interaction: discord.Interaction, case_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    row, s_row = find_warning_by_id(case_id)
    if not row: return await interaction.followup.send("❌ Case not found.")
    res = await execute_live_punishment_revocation(interaction.guild, row, str(interaction.user))
    row[COL_REVOKED], row[COL_REVOKED_BY], row[COL_REVOKED_AT] = "TRUE", str(interaction.user), str(datetime.datetime.utcnow())
    update_row(s_row, SHEET_NAME, row)
    await interaction.followup.send(f"✅ Revoked {case_id}. Server result: `{res}`")

@bot.tree.command(name="modstats", description="[Admin] Server moderation stats")
async def modstats(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    rows = read_all_rows()
    embed = discord.Embed(title="📊 Moderation Stats", description=f"Total Records: {len(rows)}", color=discord.Color.blurple())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="appeal", description="Submit an evaluation appeal")
async def appeal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cases = [(r, idx) for r, idx in get_user_warnings(interaction.user.id) if pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not cases: return await interaction.followup.send("✅ No active infractions.", ephemeral=True)
    await interaction.followup.send("Select case to appeal:", view=AppealDropdownView(cases), ephemeral=True)

@bot.tree.command(name="viewmywarnings", description="View your warnings")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=build_historical_log_embed("👤 Your History", get_user_warnings(interaction.user.id), interaction.user.display_avatar.url), ephemeral=True)

@bot.tree.command(name="viewwarnings", description="[Admin] View user warnings")
async def viewwarnings(interaction: discord.Interaction, user_target: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    tid = extract_id(user_target)
    await interaction.followup.send(embed=build_historical_log_embed(f"📋 Audit File: {tid}", get_user_warnings(tid)))

@bot.tree.command(name="warn", description="[Admin] Issue warning")
@app_commands.choices(source=[app_commands.Choice(name="Discord", value="Discord"), app_commands.Choice(name="Roblox", value="Roblox Game")])
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None, end_date: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", end_date)

@bot.tree.command(name="timeout", description="[Admin] Time out a user natively")
@app_commands.choices(unit=[app_commands.Choice(name="Minutes", value="minutes"), app_commands.Choice(name="Hours", value="hours"), app_commands.Choice(name="Days", value="days")])
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int, unit: app_commands.Choice[str]):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    td = datetime.timedelta(**{unit.value: amount})
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, timeout_duration=td)

@bot.tree.command(name="ban", description="[Admin] Ban a user")
async def ban_cmd(interaction: discord.Interaction, user_target: str, reason: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    tid = extract_id(user_target)
    m = interaction.guild.get_member(int(tid))
    await run_moderation_action(interaction, tid, str(m) if m else tid, m, reason, "Ban", "Discord", "Never")

@bot.tree.command(name="staff_suspension", description="[Admin] Suspend staff")
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    await interaction.response.defer()
    save_suspended_roles(user.id, [r.id for r in user.roles if r.name != "@everyone" and not r.managed])
    for r in user.roles:
        if r.name != "@everyone" and not r.managed and r.name not in PROTECTED_ROLE_NAMES:
            try: await user.remove_roles(r)
            except: pass
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Staff Suspension", "Discord", end_date)

@bot.tree.command(name="restoreroles", description="Restore roles if suspension expired")
async def restoreroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ids = pop_suspended_roles(interaction.user.id)
    if not ids: return await interaction.followup.send("⚠️ No backups found.", ephemeral=True)
    for rid in ids:
        r = interaction.guild.get_role(rid)
        if r and r.name not in PROTECTED_ROLE_NAMES:
            try: await interaction.user.add_roles(r)
            except: pass
    await interaction.followup.send("✅ Roles restored.", ephemeral=True)

# ── Flask Server ─────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home(): return "BWR7 Warnings Bot Online", 200

@app.route('/privacy')
def privacy(): return "Privacy Policy: Handles Roblox IDs for linking.", 200

@app.route('/terms')
def terms(): return "TOS: By verifying, you authorize ID checking.", 200

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state)
    if not did: return "❌ Session expired or invalid state.", 403
    
    try:
        token = requests.post("https://apis.roblox.com/oauth/v1/token", data={"client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": ROBLOX_REDIRECT_URI}, timeout=10).json().get("access_token")
        user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        
        # POST DIRECTLY TO VERIFY SHEET
        try:
            requests.post(VERIFY_APPEND_URL, headers=sheets_headers(), json={"values": [[str(did), str(user['sub']), user['preferred_username']]]}, timeout=10)
        except Exception as e: print(f"Sheet write error: {e}")
        
        # 🚀 RESTORED: Sync Nickname AND Send Direct Message
        async def sync_rename_and_dm():
            # 1. Send the Success DM
            try:
                u = await bot.fetch_user(int(did))
                if u:
                    embed = discord.Embed(title="✅ Account Verification Complete!", description="Your server profile has been linked and your nickname synced to your Roblox account.", color=discord.Color.green())
                    embed.add_field(name="Roblox Username", value=f"[{user['preferred_username']}](https://www.roblox.com/users/{user['sub']}/profile)", inline=True)
                    embed.add_field(name="Roblox ID", value=f"`{user['sub']}`", inline=True)
                    await u.send(embed=embed)
            except Exception as e:
                print(f"DM Error: {e}")

            # 2. Rename the user in all shared servers
            for g in bot.guilds:
                try:
                    m = await g.fetch_member(int(did))
                    if m: await m.edit(nick=user['preferred_username'][:32], reason="Auto-Verify Sync")
                except Exception as e:
                    print(f"Rename Error: {e}")
                    
        bot.loop.create_task(sync_rename_and_dm())
        
        html = f"""
        <html><head><title>Success</title><style>body {{font-family: sans-serif; background: #0f172a; color: white; text-align: center; padding-top: 100px;}} .card {{background: #1e293b; max-width: 400px; margin: auto; padding: 40px; border-radius: 12px;}} h1 {{color: #22c55e;}}</style></head>
        <body><div class="card"><h1>✅ Success!</h1><p>Roblox Account <strong>{user['preferred_username']}</strong> linked safely.</p><p style="color:#64748b;font-size:14px;">You can close this tab.</p></div></body></html>
        """
        return render_template_string(html)
    except Exception as e: return f"❌ Backend Error: {e}", 500

if __name__ != "__main__":
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
else:
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
