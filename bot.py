import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, uuid, datetime, requests, threading, json, re, asyncio, time
from flask import Flask, request, render_template_string
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── 1. CONFIGURATION ────────────────────────────────────────────────────────
# Ensure your Render Environment Variables are set:
# DISCORD_BOT_TOKEN, GOOGLE_TOKEN, ROBLOX_CLIENT_ID, ROBLOX_CLIENT_SECRET
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME, VERIFY_SHEET, STATE_SHEET = "Violations", "VerifiedUsers", "TempStates"
ROBLOX_CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI = "https://bot-h57e.onrender.com/roblox_callback"
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

# ── 2. BOT INITIALIZATION (DEFINED EARLY TO PREVENT CRASHES) ───────────
intents = discord.Intents.default(); intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── 3. AUTHENTICATION ENGINE ───────────────────────────────────────────
creds = None
if os.path.exists("service_account.json"):
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json", scopes=['https://www.googleapis.com/auth/spreadsheets']
    )

def get_headers():
    token = os.environ.get('GOOGLE_TOKEN', '')
    if creds:
        if not creds.valid: creds.refresh(Request())
        token = creds.token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── 4. CORE DATABASE HELPERS ──────────────────────────────────────────
def pad(row, length=15): return list(row) + [""] * (length - len(row))
def extract_id(s): return re.search(r'\d+', s).group(0) if re.search(r'\d+', s) else s.strip()

def append_to_sheet(url, row): 
    requests.post(url, headers=get_headers(), json={"values": [row]}, timeout=10)

def update_row(idx, sheet, row):
    rng = f"{sheet}!A{idx}:O{idx}" if sheet == SHEET_NAME else f"{sheet}!A{idx}:C{idx}"
    requests.put(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}?valueInputOption=RAW", 
                 headers=get_headers(), json={"values": [row]}, timeout=10)

def find_warning_by_id(wid):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
    rows = requests.get(url, headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        r = pad(r)
        if r[6].strip().upper() == wid.strip().upper(): return r, i + 2
    return None, None
    # ── 5. MODERATION & SUSPENSION HELPERS ────────────────────────────────
PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

def pop_suspended_roles(uid):
    if not os.path.exists("suspended_roles.json"): return []
    try:
        with open("suspended_roles.json", "r") as f: data = json.load(f)
        ids = data.pop(str(uid), [])
        with open("suspended_roles.json", "w") as f: json.dump(data, f)
        return ids
    except: return []

async def execute_live_punishment_revocation(guild, row, admin_name):
    uid, rest_type, source = int(row[0]), row[11].strip(), row[10].strip()
    if source != "Discord": return "Logged to Sheet (In-Game)"
    
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
        for rid in ids:
            r = guild.get_role(rid)
            if r and r.name not in PROTECTED_ROLE_NAMES:
                try: await guild.get_member(uid).add_roles(r)
                except: pass
        return "Roles restored"
    return "Flagged"

async def run_moderation_action(ctx, uid, name, member, reason, rtype, source, expiry, td=None):
    wid = str(uuid.uuid4())[:8].upper()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    if member:
        dm = discord.Embed(title=f"⚠️ Notice: {rtype.upper()}", description=reason, color=discord.Color.red())
        dm.add_field(name="Case ID", value=f"`{wid}`"); dm.add_field(name="Expiry", value=expiry)
        try: await member.send(embed=dm)
        except: pass

    if source == "Discord" and member:
        if rtype == "Timeout": await member.timeout(td or datetime.timedelta(days=1), reason=reason)
        elif rtype == "Ban": await ctx.guild.ban(member, reason=reason)

    append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW", 
                   [uid, name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid])
    
    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.red())
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target", value=f"<@{uid}>"); log.add_field(name="Case ID", value=f"`{wid}`")
    log.add_field(name="Reason", value=reason, inline=False)
    await ctx.followup.send(embed=log)

# ── 6. SLASH COMMANDS ──────────────────────────────────────────────────
@bot.tree.command(name="warn")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", "Discord", "Never")

@bot.tree.command(name="timeout")
async def timeout(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int):
    await interaction.response.defer()
    td = datetime.timedelta(minutes=amount)
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, td)

@bot.tree.command(name="ban")
async def ban(interaction: discord.Interaction, target: str, reason: str):
    await interaction.response.defer()
    tid = extract_id(target)
    await run_moderation_action(interaction, tid, tid, interaction.guild.get_member(int(tid)), reason, "Ban", "Discord", "Never")
    # ── 7. BACKGROUND TASKS ────────────────────────────────────────────────
@tasks.loop(hours=24)
async def expiry_sweeper():
    rows = read_all_rows()
    current_date = datetime.datetime.utcnow().date()
    for idx, raw in enumerate(rows):
        r = pad(raw)
        if r[7].upper() == "TRUE" or not r[0] or r[13] in ("Never", "", "None"): continue
        try:
            expiry = datetime.datetime.strptime(r[13].split(" ")[0], "%Y-%m-%d").date()
            if current_date >= expiry:
                for guild in bot.guilds:
                    try:
                        if r[11] == "Ban": await guild.unban(discord.Object(id=int(r[0])))
                        elif r[11] == "Timeout":
                            m = await guild.fetch_member(int(r[0]))
                            if m: await m.timeout(None)
                    except: pass
                r[7] = "TRUE"
                r[8] = "System Auto-Expiry"
                r[9] = str(datetime.datetime.utcnow())
                update_row(idx + 2, SHEET_NAME, r)
        except: continue

# ── 8. FLASK SERVER & CALLBACK ──────────────────────────────────────────
app = Flask(__name__)

def pop_oauth_state_from_cloud(token):
    # This helper is required for the callback
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(token).strip():
            if (int(time.time()) - int(r[2].strip())) > 300: return None
            update_row(i + 2, STATE_SHEET, ["", "", ""])
            return r[1].strip()
    return None

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state)
    if not did: return "❌ Session expired or invalid.", 403
    
    try:
        # 1. Exchange Code
        token_resp = requests.post("https://apis.roblox.com/oauth/v1/token", data={
            "client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, 
            "grant_type": "authorization_code", "code": code, "redirect_uri": ROBLOX_REDIRECT_URI
        }, timeout=10)
        token_data = token_resp.json()
        
        # 2. Get User
        user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", 
                            headers={"Authorization": f"Bearer {token_data['access_token']}"}, timeout=10).json()
        
        # 3. Save to Sheet
        append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C:append?valueInputOption=RAW", 
                       [str(did), str(user['sub']), user['preferred_username']])
        
        # 4. Sync Nickname
        async def sync_rename():
            for g in bot.guilds:
                try:
                    m = await g.fetch_member(int(did))
                    if m: await m.edit(nick=user['preferred_username'][:32], reason="Auto-Verify Sync")
                except: pass
        bot.loop.create_task(sync_rename())
        return "✅ Success! Account linked."
    except Exception as e: return f"❌ Backend Error: {e}", 500

# ── 9. FINAL STARTUP ───────────────────────────────────────────────────
if __name__ == "__main__":
    @bot.event
    async def on_ready():
        await bot.tree.sync()
        expiry_sweeper.start()
        print("✅ Bot is fully operational.")

    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
