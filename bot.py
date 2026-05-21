import discord, os, uuid, datetime, requests, json, re, asyncio, time, threading
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request, render_template_string
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── 1. CONFIGURATION & CREDENTIALS ─────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME = "Violations"
VERIFY_SHEET_NAME = "VerifiedUsers"
TEMP_STATE_SHEET = "TempStates"

ROBLOX_CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI = "https://bot-h57e.onrender.com/roblox_callback"
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"
ROLES_BACKUP_FILE = "suspended_roles.json"

PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

# ── 2. BOT & SERVER INIT ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)
app = Flask(__name__)

# ── 3. GOOGLE SHEETS AUTH & DATABASE HELPERS ───────────────────────────────────
def sheets_headers():
    token = os.environ.get('GOOGLE_TOKEN', '')
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def extract_id(input_string: str) -> str:
    match = re.search(r'\d+', input_string)
    return match.group(0) if match else input_string.strip()

def pad(row, length=15): return list(row) + [""] * (length - len(row))

def read_all_rows(sheet=SHEET_NAME):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet}!A:O"
    try:
        resp = requests.get(url, headers=sheets_headers(), timeout=10)
        return resp.json().get("values", [])[1:]
    except: return []

def append_to_sheet(sheet_target, row_data):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_target}!A:O:append?valueInputOption=RAW"
    try:
        resp = requests.post(url, headers=sheets_headers(), json={"values": [row_data]}, timeout=10)
        if resp.status_code != 200: print(f"[ERROR] Logging Failed: {resp.text}")
    except Exception as e: print(f"[ERROR] Connection failed: {e}")

def update_row(idx, row_data, sheet=SHEET_NAME):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet}!A{idx}:O{idx}?valueInputOption=RAW"
    try: requests.put(url, headers=sheets_headers(), json={"values": [row_data]}, timeout=10)
    except: pass

def clear_temp_state_row(idx):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{TEMP_STATE_SHEET}!A{idx}:C{idx}?valueInputOption=RAW"
    try: requests.put(url, headers=sheets_headers(), json={"values": [["", "", ""]]}, timeout=5)
    except: pass

def get_verified_roblox_id(discord_id: str) -> str:
    rows = read_all_rows(VERIFY_SHEET_NAME)
    for row in rows:
        if row and row[0].strip() == str(discord_id).strip(): return row[1].strip()
    return None

def pop_oauth_state(token: str):
    rows = read_all_rows(TEMP_STATE_SHEET)
    for i, r in enumerate(rows):
        if r and r[0].strip() == token.strip():
            clear_temp_state_row(i + 2)
            return r[1].strip()
    return None

def save_suspended_roles(user_id, role_ids):
    data = {}
    if os.path.exists(ROLES_BACKUP_FILE):
        try:
            with open(ROLES_BACKUP_FILE, "r") as f: data = json.load(f)
        except: pass
    data[str(user_id)] = role_ids
    with open(ROLES_BACKUP_FILE, "w") as f: json.dump(data, f)

def pop_suspended_roles(user_id):
    if not os.path.exists(ROLES_BACKUP_FILE): return []
    try:
        with open(ROLES_BACKUP_FILE, "r") as f: data = json.load(f)
        ids = data.pop(str(user_id), [])
        with open(ROLES_BACKUP_FILE, "w") as f: json.dump(data, f)
        return ids
    except: return []

# ── 4. PREMIUM MODERATION ENGINE ───────────────────────────────────────────────
async def run_moderation_action(ctx, uid, name, member, reason, rtype, source, expiry, td=None):
    wid = str(uuid.uuid4())[:8].upper()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # 1. High-Quality Direct Message Embed
    if member:
        dm = discord.Embed(
            title=f"⚠️ Official Notice: {rtype.upper()}", 
            description=f"A formal system action has been registered against your profile inside **{ctx.guild.name}**.", 
            color=discord.Color.red()
        )
        dm.add_field(name="📋 Infraction Type", value=rtype, inline=True)
        dm.add_field(name="🔖 Case ID", value=f"`{wid}`", inline=True)
        dm.add_field(name="⏱️ Expiration", value=expiry, inline=True)
        dm.add_field(name="📋 Stated Reason", value=f"```text\n{reason}\n```", inline=False)
        dm.add_field(name="⚖️ Appeal Process", value=f"[Open Official Appeal Form]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        dm.set_footer(text="Automated Compliance Engine • Busways Administration")
        try: await member.send(embed=dm)
        except: pass

    # 2. Execute Discord Penalty
    if source == "Discord" and member:
        if rtype == "Timeout": 
            try: await member.timeout(td or datetime.timedelta(days=1), reason=reason)
            except: pass
        elif rtype == "Ban": 
            try: await ctx.guild.ban(member, reason=reason)
            except: pass

    # 3. Log to Google Sheets
    row_payload = [str(uid), name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid]
    append_to_sheet(SHEET_NAME, row_payload)

    # 4. High-Quality Staff Audit Log Embed
    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.from_rgb(44, 62, 80))
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target User", value=f"<@{uid}>", inline=True)
    log.add_field(name="Case ID", value=f"`{wid}`", inline=True)
    log.add_field(name="Target ID", value=f"`{uid}`", inline=True)
    log.add_field(name="Reason Provided", value=f"```text\n{reason}\n```", inline=False)
    log.set_footer(text=f"Actioned by {ctx.user} • {ts}")
    await ctx.followup.send(embed=log)

# ── 5. RESTORED SLASH COMMANDS ─────────────────────────────────────────────────
@bot.tree.command(name="warn", description="Issue a formal warning")
async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: str):
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", "Discord", "Never")

@bot.tree.command(name="timeout", description="Apply a temporary timeout")
async def timeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int, unit: str):
    await interaction.response.defer()
    td_args = {unit: amount} if unit in ['minutes', 'hours', 'days'] else {'minutes': amount}
    td = datetime.timedelta(**td_args)
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, td)

@bot.tree.command(name="ban", description="Permanently ban a user")
async def ban_cmd(interaction: discord.Interaction, target_id: str, reason: str):
    await interaction.response.defer()
    target = extract_id(target_id)
    member = interaction.guild.get_member(int(target))
    await run_moderation_action(interaction, target, target, member, reason, "Ban", "Discord", "Never")

@bot.tree.command(name="send_message", description="[Admin] Dispatch a message or JSON embed to a channel")
@app_commands.describe(channel_id="ID of target channel", message="Basic text", embed_json="Valid JSON embed code")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    await interaction.response.defer(ephemeral=True)
    target_channel = interaction.guild.get_channel(int(extract_id(channel_id)))
    
    if not target_channel:
        return await interaction.followup.send("❌ **Error:** Target channel ID could not be located.")
    if not message and not embed_json:
        return await interaction.followup.send("❌ **Error:** You must provide either a text message or a formatted JSON block.")

    try:
        if embed_json:
            clean_json = re.sub(r'^```json\n|```$', '', embed_json, flags=re.MULTILINE)
            data = json.loads(clean_json.strip())
            embed_dict = data["embeds"][0] if "embeds" in data and isinstance(data["embeds"], list) and len(data["embeds"]) > 0 else data
            target_embed = discord.Embed.from_dict(embed_dict)
            await target_channel.send(content=message, embed=target_embed)
        else:
            await target_channel.send(content=message)
        await interaction.followup.send("✅ Announcement dispatched successfully.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ **JSON Formatting Error:** {str(e)}", ephemeral=True)

@bot.tree.command(name="restoreroles", description="[Admin] Restore suspended staff roles")
async def restoreroles(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    role_ids = pop_suspended_roles(user.id)
    if not role_ids: return await interaction.followup.send("❌ No saved roles found for this user.", ephemeral=True)
    
    restored = 0
    for rid in role_ids:
        role = interaction.guild.get_role(rid)
        if role and role.name not in PROTECTED_ROLE_NAMES:
            try: 
                await user.add_roles(role)
                restored += 1
            except: pass
    await interaction.followup.send(f"✅ Restored **{restored}** staff roles cleanly.", ephemeral=True)

@bot.tree.command(name="history", description="View official moderation history for a user")
async def history(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    rows = read_all_rows()
    warnings = [r for r in rows if r and r[0].strip() == str(user.id)]
    
    embed = discord.Embed(title=f"📋 Official Audit Record: {user.display_name}", color=discord.Color.from_rgb(44, 62, 80))
    embed.set_thumbnail(url=user.display_avatar.url)
    
    if not warnings:
        embed.description = "✅ No infractions found on official record."
    else:
        for w in warnings[-5:]: # Shows last 5 infractions
            w = pad(w)
            status = "🟢 Revoked" if w[7].upper() == "TRUE" else "🔴 Active"
            embed.add_field(
                name=f"Case {w[6]} | {w[11]}", 
                value=f"**Date:** {w[5]}\n**Reason:** {w[4]}\n**Status:** {status}", 
                inline=False
            )
    embed.set_footer(text="Automated Compliance Engine • Busways Administration")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="checklink", description="Check your current Roblox account link status")
async def checklink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    roblox_id = get_verified_roblox_id(interaction.user.id)
    if roblox_id:
        await interaction.followup.send(f"✅ Your account is officially linked to Roblox ID: **{roblox_id}**", ephemeral=True)
    else:
        await interaction.followup.send("❌ No official link found. Please run `/verify`.", ephemeral=True)

@bot.tree.command(name="verify", description="Link your Roblox account securely using the official Roblox Prompt")
async def verify(interaction: discord.Interaction):
    # IMMEDIATELY defer to prevent 404 Unknown Interaction crashes
    await interaction.response.defer(ephemeral=True)
    
    if get_verified_roblox_id(interaction.user.id):
        return await interaction.followup.send("⚠️ **Account Linked Already:** Your profile is already registered.", ephemeral=True)

    state_token = str(uuid.uuid4())
    # Save state to Google Sheets to survive Render sleeping/rebooting
    append_to_sheet(TEMP_STATE_SHEET, [state_token, str(interaction.user.id), str(int(time.time()))])
    
    roblox_oauth_url = (
        f"https://apis.roblox.com/oauth/v1/authorize"
        f"?client_id={ROBLOX_CLIENT_ID}&redirect_uri={ROBLOX_REDIRECT_URI}"
        f"&scope=openid+profile&response_type=code&state={state_token}"
    )
    
    embed = discord.Embed(
        title="🔐 Official Roblox Account Verification",
        description="Click the official secure authorization link below to securely bind your Roblox coordinates directly to your server profile.",
        color=discord.Color.orange()
    )
    embed.add_field(name="📋 Verification Link", value=f"🔗 **[Click Here to Link Roblox Account]({roblox_oauth_url})**", inline=False)
    embed.set_footer(text="Powered by Official Roblox OAuth2 Security Framework")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── 6. BACKGROUND EXPIRY SWEEPER ───────────────────────────────────────────────
@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    print("[Sweeper] Starting automated infraction expiration analysis...")
    rows = read_all_rows()
    if not rows: return

    current_date = datetime.datetime.utcnow().date()
    for idx, raw in enumerate(rows):
        row = pad(raw)
        user_id_str, is_revoked, restriction_type, expiry_str = row[0].strip(), row[7].upper() == "TRUE", row[11].strip(), row[13].strip()

        if is_revoked or not user_id_str or expiry_str in ("Never", "", "None"): continue

        try:
            expiry_date = datetime.datetime.strptime(expiry_str.split(" ")[0].strip(), "%Y-%m-%d").date()
            if current_date >= expiry_date:
                print(f"[Sweeper] Auto-lifting {restriction_type} for {user_id_str}")
                for guild in bot.guilds:
                    if restriction_type == "Ban":
                        try: await guild.unban(discord.Object(id=int(user_id_str)), reason="System Auto-Expiry")
                        except: pass
                    elif restriction_type == "Timeout":
                        try:
                            member = await guild.fetch_member(int(user_id_str))
                            if member: await member.timeout(None, reason="System Auto-Expiry")
                        except: pass
                
                row[7] = "TRUE"
                row[8] = "System Auto-Expiry"
                row[9] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                update_row(idx + 2, row)
                await asyncio.sleep(1)
        except: continue

# ── 7. PRODUCTION FLASK SERVER & CALLBACK ──────────────────────────────────────
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

@app.route('/roblox_callback')
def roblox_callback():
    auth_code, returned_state = request.args.get("code"), request.args.get("state")
    if not auth_code or not returned_state: return "❌ Missing parameters.", 400

    # Retrieve state securely from Google Sheets
    discord_user_id = pop_oauth_state(returned_state)
    if not discord_user_id: 
        return "❌ Session expired or invalid. Please run /verify again in Discord.", 403

    try:
        token_resp = requests.post("https://apis.roblox.com/oauth/v1/token", data={
            "client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, 
            "grant_type": "authorization_code", "code": auth_code, "redirect_uri": ROBLOX_REDIRECT_URI
        }, timeout=10)
        
        if token_resp.status_code != 200: return "❌ Token request failure.", 500
        access_token = token_resp.json().get("access_token")
        
        userinfo_resp = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        user_info_data = userinfo_resp.json()
        roblox_id, roblox_name = user_info_data.get("sub"), user_info_data.get("preferred_username")
        
        append_to_sheet(VERIFY_SHEET_NAME, [str(discord_user_id), str(roblox_id), roblox_name])
        
        # Dispatch DM to user securely from Flask thread
        async def send_dm():
            try:
                user = await bot.fetch_user(int(discord_user_id))
                if user:
                    embed = discord.Embed(title="✅ Account Verification Complete!", description="Your server profile has been linked to the official Roblox registry database.", color=discord.Color.green())
                    embed.add_field(name="Roblox Username", value=f"[{roblox_name}](https://www.roblox.com/users/{roblox_id}/profile)", inline=True)
                    embed.add_field(name="Account User ID", value=f"`{roblox_id}`", inline=True)
                    await user.send(embed=embed)
            except: pass
        
        # Thread-safe execution for Discord task
        asyncio.run_coroutine_threadsafe(send_dm(), bot.loop)
        
        return render_template_string(SUCCESS_HTML_PAGE, username=roblox_name)
    except Exception as e: return f"❌ System callback error: {str(e)}", 500

# ── 8. MAIN THREAD EXECUTION ───────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not automatic_expiry_sweeper.is_running():
        automatic_expiry_sweeper.start()
    print(f"✅ Bot Online & Synced as {bot.user}")

if __name__ == "__main__":
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
