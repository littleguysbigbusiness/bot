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
VERIFY_APPEND_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{VERIFY_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
STATE_READ_URL    = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET_NAME}!A:C"
STATE_APPEND_URL  = f"https://sheets.googleapis.com/v4/spreadsheets/{STATE_SHEET_NAME}!A:C:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"

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
    creds = service_account.Credentials.from_service_account_file(TARGET_PATH, scopes=['https://www.googleapis.com/auth/spreadsheets'])

def sheets_headers():
    global creds
    if creds and not creds.valid: creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token if creds else ''}", "Content-Type": "application/json"}

# ── Helpers ──────────────────────────────────────────────────────────────────
def pad(row, length=15): return list(row) + [""] * (length - len(row))
def extract_id(s): return re.search(r'\d+', s).group(0) if re.search(r'\d+', s) else s.strip()

def read_all_rows():
    try: return requests.get(SHEET_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])[1:]
    except: return []

def append_row(row): requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
def update_row(idx, row): requests.put(f"{SHEET_UPDATE_BASE}{SHEET_NAME}!A{idx}:O{idx}?valueInputOption=RAW", headers=sheets_headers(), json={"values": [row]}, timeout=10)

def find_warning_by_id(wid):
    for i, r in enumerate(read_all_rows()):
        r = pad(r)
        if r[COL_INCIDENT_ID].strip().upper() == wid.strip().upper(): return r, i + 2
    return None, None

def get_user_warnings(uid): return [(pad(r), i + 2) for i, r in enumerate(read_all_rows()) if pad(r)[COL_USER_ID].strip() == str(uid).strip()]

# ── State Persistence Helpers ────────────────────────────────────────────────
def log_verified_user(did, rid, rname): requests.post(VERIFY_APPEND_URL, headers=sheets_headers(), json={"values": [[str(did), str(rid), str(rname)]]}, timeout=10)
def get_verified_roblox_id(did):
    try:
        rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
        for r in rows[1:]:
            if r and r[0].strip() == str(did).strip(): return r[1].strip()
    except: pass
    return None

def save_oauth_state_to_cloud(token, did): requests.post(STATE_APPEND_URL, headers=sheets_headers(), json={"values": [[str(token), str(did), str(int(time.time()))]]}, timeout=10)

def pop_oauth_state_from_cloud(token):
    for attempt in range(5):
        try:
            rows = requests.get(STATE_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
            if rows and len(rows) > 1:
                for i, r in enumerate(rows[1:]):
                    if r and r[0].strip() == str(token).strip():
                        did = r[1].strip()
                        if (int(time.time()) - int(r[2])) > 300: return None
                        requests.put(f"{SHEET_UPDATE_BASE}{STATE_SHEET_NAME}!A{i+2}:C{i+2}?valueInputOption=RAW", headers=sheets_headers(), json={"values": [["", "", ""]]}, timeout=5)
                        return did
            time.sleep(1)
        except: pass
    return None

# ── Bot Core ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default(); intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print("✅ Bot Ready & Synced.")
    if not automatic_expiry_sweeper.is_running(): automatic_expiry_sweeper.start()

# ── Tasks ─────────────────────────────────────────────────────────────────────
@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    for idx, r in enumerate(read_all_rows()):
        r = pad(r)
        if r[COL_REVOKED].upper() == "TRUE" or not r[COL_USER_ID]: continue
        try:
            expiry = datetime.datetime.strptime(r[COL_END_DATE].split(" ")[0], "%Y-%m-%d").date()
            if datetime.datetime.utcnow().date() >= expiry:
                for guild in bot.guilds:
                    try:
                        if r[COL_RESTRICTION] == "Ban": await guild.unban(discord.Object(id=int(r[COL_USER_ID])))
                        elif r[COL_RESTRICTION] == "Timeout": 
                            m = await guild.fetch_member(int(r[COL_USER_ID]))
                            if m: await m.timeout(None)
                    except: pass
                r[COL_REVOKED], r[COL_REVOKED_BY], r[COL_REVOKED_AT] = "TRUE", "System Auto-Expiry", str(datetime.datetime.utcnow())
                update_row(idx + 2, r)
        except: continue

# ── Flask Server ─────────────────────────────────────────────────────────────
app = Flask(__name__)
@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state)
    if not did: return "❌ Session expired or invalid.", 403
    
    token = requests.post("https://apis.roblox.com/oauth/v1/token", data={"client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": ROBLOX_REDIRECT_URI}).json().get("access_token")
    user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {token}"}).json()
    
    log_verified_user(did, user['sub'], user['preferred_username'])
    
    async def sync_rename():
        for g in bot.guilds:
            try:
                m = await g.fetch_member(int(did))
                await m.edit(nick=user['preferred_username'][:32])
            except: pass
    bot.loop.create_task(sync_rename())
    return "✅ Success! Account linked."

# ── Admin Slash Commands ─────────────────────────────────────────────────────
@bot.tree.command(name="setprefix")
async def setprefix(interaction: discord.Interaction, prefix: str):
    if len(prefix.strip()) > 5 or re.sub(r'[^A-Za-z0-9]', '', prefix).upper() in ("CEO", "VCEO"):
        return await interaction.response.send_message("❌ Invalid or restricted prefix.", ephemeral=True)
    base = interaction.user.display_name.split(" - ")[-1]
    new = f"{prefix.strip()} - {base}"
    try:
        await interaction.user.edit(nick=new[:32])
        await interaction.response.send_message(f"✅ Nickname: `{new[:32]}`", ephemeral=True)
    except: await interaction.response.send_message("❌ Forbidden.", ephemeral=True)

@bot.tree.command(name="verify")
async def verify(interaction: discord.Interaction):
    token = str(uuid.uuid4())
    save_oauth_state_to_cloud(token, interaction.user.id)
    url = f"https://apis.roblox.com/oauth/v1/authorize?client_id={ROBLOX_CLIENT_ID}&redirect_uri={ROBLOX_REDIRECT_URI}&scope=openid+profile&response_type=code&state={token}"
    await interaction.response.send_message(f"🔗 [Link Account]({url})", ephemeral=True)

# ── Boot Sequence ────────────────────────────────────────────────────────────
if __name__ != "__main__":
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
else:
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
