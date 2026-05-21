import discord, os, uuid, datetime, requests, json, re, asyncio, time, threading
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request, render_template_string
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── 1. CONFIGURATION & CONSTANTS ───────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME = "Violations"
VERIFY_SHEET = "VerifiedUsers"
ROBLOX_CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI = "https://bot-h57e.onrender.com/roblox_callback"
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

STATE_STORE = {} # Secures Roblox OAuth sessions
PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

# ── 2. INITIALIZATION & HELPERS ────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)

def get_headers():
    return {"Authorization": f"Bearer {os.environ.get('GOOGLE_TOKEN', '')}", "Content-Type": "application/json"}

def extract_id(input_string: str) -> str:
    match = re.search(r'\d+', input_string)
    return match.group(0) if match else input_string.strip()

def pad(row, length=15):
    return list(row) + [""] * (length - len(row))

def read_all_rows():
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
        return requests.get(url, headers=get_headers(), timeout=10).json().get("values", [])[1:]
    except: return []

def append_to_sheet(sheet, row_data):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet}!A:O:append?valueInputOption=RAW"
    requests.post(url, headers=get_headers(), json={"values": [row_data]}, timeout=10)

def update_row(idx, sheet, row_data):
    rng = f"{sheet}!A{idx}:O{idx}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}?valueInputOption=RAW"
    requests.put(url, headers=get_headers(), json={"values": [row_data]}, timeout=10)

def pop_suspended_roles(uid):
    if not os.path.exists("suspended_roles.json"): return []
    try:
        with open("suspended_roles.json", "r") as f: data = json.load(f)
        ids = data.pop(str(uid), [])
        with open("suspended_roles.json", "w") as f: json.dump(data, f)
        return ids
    except: return []

# ── 3. MODERATION ENGINE ───────────────────────────────────────────────────────
async def run_moderation_action(ctx, uid, name, member, reason, rtype, source, expiry, td=None):
    wid = str(uuid.uuid4())[:8].upper()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Send user a professional DM notice
    if member:
        dm = discord.Embed(title=f"⚠️ Official Notice: {rtype.upper()}", 
                           description=f"A formal action has been registered against your profile in **{ctx.guild.name}**.", 
                           color=discord.Color.red())
        dm.add_field(name="Case ID", value=f"`{wid}`", inline=True)
        dm.add_field(name="Expiration", value=expiry, inline=True)
        dm.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
        dm.add_field(name="Appeal", value=f"[Click here to appeal]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        try: await member.send(embed=dm)
        except: pass

    # Execute Discord punishment
    if source == "Discord" and member:
        if rtype == "Timeout": await member.timeout(td or datetime.timedelta(days=1), reason=reason)
        elif rtype == "Ban": await ctx.guild.ban(member, reason=reason)

    # Log to Google Sheets
    append_to_sheet(SHEET_NAME, [str(uid), name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid])

    # Send public/staff log embed
    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.red())
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target", value=f"<@{uid}>", inline=True)
    log.add_field(name="Case ID", value=f"`{wid}`", inline=True)
    log.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    await ctx.followup.send(embed=log)

# ── 4. SLASH COMMANDS ──────────────────────────────────────────────────────────
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

@bot.tree.command(name="send_message", description="[Admin] Dispatch a rich embed or text message")
async def send_message(interaction: discord.Interaction, channel_id: str, message: str = None, embed_json: str = None):
    await interaction.response.defer(ephemeral=True)
    chan = interaction.guild.get_channel(int(extract_id(channel_id)))
    if not chan: return await interaction.followup.send("❌ Channel not found.")
    
    try:
        if embed_json:
            data = json.loads(embed_json)
            embed = discord.Embed.from_dict(data["embeds"][0] if "embeds" in data else data)
            await chan.send(content=message, embed=embed)
        else:
            await chan.send(content=message)
        await interaction.followup.send("✅ Dispatched successfully.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error parsing JSON: {e}")

@bot.tree.command(name="restoreroles", description="[Admin] Restore suspended staff roles")
async def restoreroles(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    role_ids = pop_suspended_roles(user.id)
    if not role_ids: return await interaction.followup.send("❌ No saved roles found.")
    
    restored = 0
    for rid in role_ids:
        role = interaction.guild.get_role(rid)
        if role and role.name not in PROTECTED_ROLE_NAMES:
            try: 
                await user.add_roles(role)
                restored += 1
            except: pass
    await interaction.followup.send(f"✅ Restored {restored} staff roles cleanly.", ephemeral=True)

@bot.tree.command(name="verify", description="Link your Roblox account securely")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    state_token = str(uuid.uuid4())
    STATE_STORE[state_token] = interaction.user.id
    
    url = f"https://apis.roblox.com/oauth/v1/authorize?client_id={ROBLOX_CLIENT_ID}&redirect_uri={ROBLOX_REDIRECT_URI}&scope=openid+profile&response_type=code&state={state_token}"
    
    embed = discord.Embed(title="🔐 Official Roblox Verification", description="Bind your Roblox coordinates to your server profile.", color=discord.Color.orange())
    embed.add_field(name="Verification Link", value=f"🔗 **[Click Here to Link Account]({url})**", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── 5. BACKGROUND TASKS ────────────────────────────────────────────────────────
@tasks.loop(hours=24)
async def automatic_expiry_sweeper():
    print("[Sweeper] Starting automated infraction expiration analysis...")
    rows = read_all_rows()
    if not rows: return

    current_date = datetime.datetime.utcnow().date()
    for idx, raw in enumerate(rows):
        row = pad(raw)
        user_id = row[0].strip()
        is_revoked = row[7].upper() == "TRUE"
        rtype = row[11].strip()
        expiry_str = row[13].strip()

        if is_revoked or not user_id or expiry_str in ("Never", "", "None"): continue

        try:
            exp_date = datetime.datetime.strptime(expiry_str.split(" ")[0].strip(), "%Y-%m-%d").date()
            if current_date >= exp_date:
                for guild in bot.guilds:
                    if rtype == "Ban":
                        try: await guild.unban(discord.Object(id=int(user_id)), reason="System Auto-Expiry")
                        except: pass
                    elif rtype == "Timeout":
                        try:
                            m = await guild.fetch_member(int(user_id))
                            if m: await m.timeout(None, reason="System Auto-Expiry")
                        except: pass
                
                row[7] = "TRUE"
                row[8] = "System Auto-Expiry"
                row[9] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                update_row(idx + 2, SHEET_NAME, row)
                await asyncio.sleep(1)
        except: continue

# ── 6. FLASK WEB SERVER ────────────────────────────────────────────────────────
SUCCESS_HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Verification Complete</title>
    <style>
        body { font-family: sans-serif; background-color: #0f172a; color: #f8fafc; text-align: center; padding-top: 100px; }
        .card { background-color: #1e293b; max-width: 450px; margin: 0 auto; padding: 40px; border-radius: 12px; border: 1px solid #334155; }
        h1 { color: #22c55e; }
        p { color: #94a3b8; }
    </style>
</head>
<body>
    <div class="card">
        <h1>✅ Verification Success!</h1>
        <p>Your Roblox account <strong>{{ username }}</strong> is now linked.</p>
        <span style="color: #64748b; font-size: 14px;">You can safely close this window.</span>
    </div>
</body>
</html>
"""

@app.route('/')
def home(): 
    return "BWR7 Warnings Bot is Online and Framework is Stable!", 200

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = STATE_STORE.pop(state, None)
    if not code or not did: return "❌ Session expired or invalid.", 403
    
    try:
        # Exchange Code
        token_resp = requests.post("https://apis.roblox.com/oauth/v1/token", data={
            "client_id": ROBLOX_CLIENT_ID, "client_secret": ROBLOX_CLIENT_SECRET, 
            "grant_type": "authorization_code", "code": code, "redirect_uri": ROBLOX_REDIRECT_URI
        }, timeout=10)
        access_token = token_resp.json().get("access_token")
        
        # Get User Info
        user_data = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=10).json()
        r_id, r_name = user_data.get("sub"), user_data.get("preferred_username")
        
        # Log to Sheet
        append_to_sheet(VERIFY_SHEET, [str(did), str(r_id), r_name])
        
        # Async Discord DM
        async def notify():
            try:
                user = await bot.fetch_user(int(did))
                embed = discord.Embed(title="✅ Verification Complete!", description="Your profile is linked.", color=discord.Color.green())
                embed.add_field(name="Roblox Username", value=r_name)
                await user.send(embed=embed)
            except: pass
        bot.loop.create_task(notify())
        
        return render_template_string(SUCCESS_HTML_PAGE, username=r_name)
    except Exception as e: return f"❌ Backend Error: {e}", 500

# ── 7. MAIN EXECUTION ──────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not automatic_expiry_sweeper.is_running():
        automatic_expiry_sweeper.start()
    print(f"✅ Bot Online & Synced as {bot.user}")

if __name__ == "__main__":
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
