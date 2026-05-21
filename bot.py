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

# ── Config & Constants ────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID    = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME        = "Violations"
VERIFY_SHEET_NAME = "VerifiedUsers"
STATE_SHEET_NAME  = "TempStates"
STATUS_PAGE_URL   = "https://bwr7s.statuspage.io/api/v2/summary.json"
STATUS_CHANNEL_ID = 1476812926521184276
STATIC_STATUS_ID  = 1505808587807789117
APPEAL_CHANNEL_ID = 1505891264032149574
ROBLOX_CLIENT_ID  = os.environ.get("ROBLOX_CLIENT_ID", "")
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
    except Exception as e: print(f"❌ Auth Error: {e}")

def sheets_headers():
    global creds
    if creds and not creds.valid: creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token if creds else ''}", "Content-Type": "application/json"}

# ── Database & State Helpers ──────────────────────────────────────────────────
def extract_id(s): return re.search(r'\d+', s).group(0) if re.search(r'\d+', s) else s.strip()
def pad(row, length=15): return list(row) + [""] * (length - len(row))
def read_all_rows():
    try: return requests.get(SHEET_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])[1:]
    except: return []

def append_row(row): requests.post(SHEET_APPEND_URL, headers=sheets_headers(), json={"values": [row]}, timeout=10)
def update_row(idx, sheet, row):
    rng = f"{sheet}!A{idx}:O{idx}" if sheet == SHEET_NAME else f"{sheet}!A{idx}:C{idx}"
    requests.put(f"{SHEET_UPDATE_BASE}{rng}?valueInputOption=RAW", headers=sheets_headers(), json={"values": [row]}, timeout=10)

def find_warning_by_id(wid):
    for i, r in enumerate(read_all_rows()):
        r = pad(r)
        if r[COL_INCIDENT_ID].strip().upper() == wid.strip().upper(): return r, i + 2
    return None, None

def get_user_warnings(uid): return [(pad(r), i + 2) for i, r in enumerate(read_all_rows()) if pad(r)[COL_USER_ID].strip() == str(uid).strip()]

def get_verified_roblox_id(did):
    try:
        rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
        for r in rows[1:]:
            if r and r[0].strip() == str(did).strip(): return r[1].strip()
    except: pass
    return None

def pop_oauth_state_from_cloud(token):
    for attempt in range(5):
        try:
            rows = requests.get(STATE_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
            if rows and len(rows) > 1:
                for i, r in enumerate(rows[1:]):
                    if r and r[0].strip() == str(token).strip():
                        if (int(time.time()) - int(r[2].strip())) > 300: return None
                        update_row(i + 2, STATE_SHEET_NAME, ["", "", ""])
                        return r[1].strip()
            time.sleep(1)
        except: pass
    return None

# ── Bot Class ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default(); intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print("✅ Bot Ready.")

# ── Moderation & Commands (Truncated to fit, see Part 2 for full logic) ───────
# (I am splitting this because the full code exceeds character limits)
dm = discord.Embed(title=f"⚠️ Account Moderation Notice: {rtype.upper()}", description=f"A formal system action has been registered against your profile inside **{ctx.guild.name}**.", color=discord.Color.from_rgb(44, 62, 80))
        dm.add_field(name="📋 Infraction Type", value=rtype, inline=True)
        dm.add_field(name="📋 Stated Reason", value=f"```text\n{reason}\n```", inline=False)
        dm.add_field(name="Case ID", value=f"`{wid}`", inline=True)
        dm.add_field(name="Platform", value=source, inline=True)
        dm.add_field(name="Expiration", value=expiry, inline=True)
        dm.add_field(name="⚖️ Appeal", value=f"[Open Appeal Form]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        try: await member.send(embed=dm)
        except: pass

    if source == "Discord" and member:
        if rtype == "Timeout": await member.timeout(td or datetime.timedelta(days=1), reason=reason)
        elif rtype == "Ban": await ctx.guild.ban(member, reason=reason)

    append_row([uid, name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid])
    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.from_rgb(44, 62, 80))
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target", value=f"<@{uid}>"); log.add_field(name="Case ID", value=f"`{wid}`")
    log.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    await ctx.followup.send(embed=log)

def build_historical_log_embed(title, warnings, thumb=None):
    embed = discord.Embed(title=title, color=discord.Color.from_rgb(44, 62, 80))
    if thumb: embed.set_thumbnail(url=thumb)
    act, rev = "", ""
    for r, _ in warnings:
        block = f"▪ Case {r[COL_INCIDENT_ID].strip()} | {r[COL_RESTRICTION].strip()} | {r[COL_SOURCE].strip()}\n  Reason: {r[COL_REASON].strip()}\n  Issued: {r[COL_TIMESTAMP][:10]} | Expires: {r[COL_END_DATE].strip()}\n"
        if r[COL_REVOKED].strip().upper() == "TRUE": rev += block + f"  ❌ REVOKED: {r[COL_REVOKED_BY].strip()}\n\n"
        else: act += block + "\n"
    embed.add_field(name="⚠️ Active", value=f"```text\n{act.strip() or 'None'}\n```", inline=False)
    embed.add_field(name="✅ Archive", value=f"```text\n{rev.strip() or 'None'}\n```", inline=False)
    return embed

# ── Slash Commands ──────────────────────────────────────────────────────────
@bot.tree.command(name="setprefix")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)
    clean = prefix.strip()
    if not clean or len(clean) > 5 or re.sub(r'[^A-Za-z0-9]', '', clean).upper() in ("CEO", "VCEO"):
        return await interaction.followup.send("❌ Invalid/Restricted prefix (Max 5 chars).", ephemeral=True)
    
    base = interaction.user.display_name.split(" - ")[-1].strip()
    new = f"{clean} - {base}"
    try:
        await interaction.user.edit(nick=new[:32])
        await interaction.followup.send(f"✅ Nickname: `{new[:32]}`", ephemeral=True)
    except: await interaction.followup.send("❌ Error: Check bot role hierarchy.", ephemeral=True)

@bot.tree.command(name="verify")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if get_verified_roblox_id(interaction.user.id): return await interaction.followup.send("⚠️ Already linked.", ephemeral=True)
    token = str(uuid.uuid4())
    save_oauth_state_to_cloud(token, interaction.user.id)
    url = f"https://apis.roblox.com/oauth/v1/authorize?client_id={ROBLOX_CLIENT_ID}&redirect_uri={ROBLOX_REDIRECT_URI}&scope=openid+profile&response_type=code&state={token}"
    await interaction.followup.send(f"🔗 [Link Account]({url})", ephemeral=True)

@bot.tree.command(name="checklink")
async def checklink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    r = get_verified_roblox_id(interaction.user.id)
    if r: await interaction.followup.send(f"✅ Linked to ID: `{r}`", ephemeral=True)
    else: await interaction.followup.send("❌ No link found.", ephemeral=True)

@bot.tree.command(name="unlink")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(interaction.user.id):
            update_row(i + 2, VERIFY_SHEET_NAME, ["", "", ""])
            return await interaction.followup.send("✅ Success.", ephemeral=True)
    await interaction.followup.send("❌ No link.", ephemeral=True)

@bot.tree.command(name="forceunlink")
async def forceunlink(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction): return
    await interaction.response.defer(ephemeral=True)
    rows = requests.get(VERIFY_READ_URL, headers=sheets_headers(), timeout=10).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(user.id):
            update_row(i + 2, VERIFY_SHEET_NAME, ["", "", ""])
            return await interaction.followup.send(f"✅ Removed link for {user.mention}.", ephemeral=True)
    await interaction.followup.send("❌ Not linked.", ephemeral=True)

@bot.tree.command(name="send_message")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    if not is_admin(interaction): return
    await interaction.response.defer(ephemeral=True)
    tc = bot.get_channel(int(extract_id(channel_id)))
    if not tc: return await interaction.followup.send("❌ Invalid Channel.")
    emb = None
    if embed_json:
        try: emb = discord.Embed.from_dict(json.loads(embed_json.strip("`").removeprefix("json")))
        except: return await interaction.followup.send("❌ Invalid JSON.")
    try: await tc.send(content=message, embed=emb); await interaction.followup.send("✅ Sent.")
    except Exception as e: await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="revokeaction")
async def revokeaction(interaction: discord.Interaction, case_id: str):
    if not is_admin(interaction): return
    await interaction.response.defer()
    row, s_row = find_warning_by_id(case_id)
    if not row: return await interaction.followup.send("❌ Not found.")
    await execute_live_punishment_revocation(interaction.guild, row, str(interaction.user))
    row[COL_REVOKED], row[COL_REVOKED_BY], row[COL_REVOKED_AT] = "TRUE", str(interaction.user), str(datetime.datetime.utcnow())
    update_row(s_row, SHEET_NAME, row)
    await interaction.followup.send(f"✅ Revoked {case_id}.")

@bot.tree.command(name="modstats")
async def modstats(interaction: discord.Interaction):
    if not is_admin(interaction): return
    await interaction.response.defer()
    await interaction.followup.send(f"📊 Total Records: {len(read_all_rows())}")

@bot.tree.command(name="appeal")
async def appeal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cases = [(r, idx) for r, idx in get_user_warnings(interaction.user.id) if pad(r)[COL_REVOKED].upper() != "TRUE"]
    if not cases: return await interaction.followup.send("✅ No active cases.", ephemeral=True)
    await interaction.followup.send("Select case:", view=AppealDropdownView(cases), ephemeral=True)

@bot.tree.command(name="viewmywarnings")
async def viewmywarnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=build_historical_log_embed("👤 History", get_user_warnings(interaction.user.id), interaction.user.display_avatar.url), ephemeral=True)

@bot.tree.command(name="viewwarnings")
async def viewwarnings(interaction: discord.Interaction, user_target: str):
    if not is_admin(interaction): return
    await interaction.response.defer()
    tid = extract_id(user_target)
    await interaction.followup.send(embed=build_historical_log_embed(f"📋 Audit: {tid}", get_user_warnings(tid)))

@bot.tree.command(name="warn")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None):
    if not is_admin(interaction): return
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", "Never")

@bot.tree.command(name="timeout")
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int, unit: app_commands.Choice[str]):
    if not is_admin(interaction): return
    await interaction.response.defer()
    td = datetime.timedelta(**{unit.value: amount})
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, td)

@bot.tree.command(name="ban")
async def ban_cmd(interaction: discord.Interaction, user_target: str, reason: str):
    if not is_admin(interaction): return
    await interaction.response.defer()
    tid = extract_id(user_target)
    m = interaction.guild.get_member(int(tid))
    await run_moderation_action(interaction, tid, str(m) if m else tid, m, reason, "Ban", "Discord", "Never")

@bot.tree.command(name="staff_suspension")
async def staff_suspension(interaction: discord.Interaction, user: discord.Member, reason: str, end_date: str):
    if not is_admin(interaction): return
    await interaction.response.defer()
    save_suspended_roles(user.id, [r.id for r in user.roles if r.name != "@everyone" and not r.managed])
    for r in user.roles:
        if r.name != "@everyone" and not r.managed and r.name not in PROTECTED_ROLE_NAMES:
            try: await user.remove_roles(r)
            except: pass
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Staff Suspension", "Discord", end_date)

@bot.tree.command(name="restoreroles")
async def restoreroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ids = pop_suspended_roles(interaction.user.id)
    if not ids: return await interaction.followup.send("⚠️ No backups.", ephemeral=True)
    for rid in ids:
        r = interaction.guild.get_role(rid)
        if r and r.name not in PROTECTED_ROLE_NAMES:
            try: await interaction.user.add_roles(r)
            except: pass
    await interaction.followup.send("✅ Restored.", ephemeral=True)

# ── Flask Server ─────────────────────────────────────────────────────────────
app = Flask(__name__)
@app.route('/')
def home(): return "BWR7 Admin Bot Online", 200

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state)
    if not did: return "❌ Session expired.", 403
    
    try:
        token = requests.post("https://apis.roblox.com/oauth/v1/token", data={"client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": ROBLOX_REDIRECT_URI}, timeout=10).json().get("access_token")
        user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        requests.post(VERIFY_APPEND_URL, headers=sheets_headers(), json={"values": [[str(did), str(user['sub']), user['preferred_username']]]})
        
        async def sync():
            for g in bot.guilds:
                try:
                    m = await g.fetch_member(int(did))
                    if m: 
                        await m.edit(nick=user['preferred_username'][:32], reason="Auto-Verify Sync")
                        embed = discord.Embed(title="✅ Verified!", description=f"Linked to {user['preferred_username']}", color=discord.Color.green())
                        await m.send(embed=embed)
                except: pass
        bot.loop.create_task(sync())
        return "✅ Success."
    except Exception as e: return f"❌ Backend Error: {e}", 500

if __name__ != "__main__":
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
else:
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
