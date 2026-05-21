import discord, os, uuid, datetime, requests, threading, json, re, asyncio, time
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request, render_template_string
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── 1. CORE CONFIGURATION & CONSTANTS ──────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME, VERIFY_SHEET, STATE_SHEET = "Violations", "VerifiedUsers", "TempStates"
ROBLOX_CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI = "https://bot-h57e.onrender.com/roblox_callback"
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

# Role Immunity List
PROTECTED_ROLE_NAMES = [
    "Rythm", "TTS Bot", "GiveawayBot", "Appy", "Application Blacklist...", "Busways OGS",
    "Near/Lived/Lives/R7", "He’s A Great Guy I Th...", "Service Pings", "astras Playhouse Key",
    "TTS", "Muted", "Security", "Warning 1", "Warning 2", "Warning 3", "Strike 1", "Strike 2",
    "Strike 3", "Staff Blacklisted", "Busways Assistance", "Partner", "Former Staff", 
    "P-Passenger", "Dev Pings", "Giveaway Pings", "Application Pings", "Dyno", "Quark Logger", 
    "Tickets v2", "BD Department", "BM Department"
]

# ── 2. DATABASE AUTH & HELPERS ──────────────────────────────────────────
def get_headers():
    token = os.environ.get('GOOGLE_TOKEN', '')
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def pad(row, length=15): return list(row) + [""] * (length - len(row))

def read_all_rows():
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O"
        return requests.get(url, headers=get_headers(), timeout=10).json().get("values", [])[1:]
    except: return []

def append_to_sheet(url, row): 
    requests.post(url, headers=get_headers(), json={"values": [row]}, timeout=10)

def update_row(idx, sheet, row):
    rng = f"{sheet}!A{idx}:O{idx}" if sheet == SHEET_NAME else f"{sheet}!A{idx}:C{idx}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}?valueInputOption=RAW"
    requests.put(url, headers=get_headers(), json={"values": [row]}, timeout=10)

def find_warning_by_id(wid):
    for i, r in enumerate(read_all_rows()):
        r = pad(r)
        if r[6].strip().upper() == wid.strip().upper(): return r, i + 2
    return None, None
    # ── 3. BOT INITIALIZATION ──────────────────────────────────────────────
intents = discord.Intents.default(); intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── 4. MODERATION ENGINE ───────────────────────────────────────────────
async def run_moderation_action(ctx, uid, name, member, reason, rtype, source, expiry, td=None):
    wid = str(uuid.uuid4())[:8].upper()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Rich Embed Notice
    if member:
        dm = discord.Embed(title=f"⚠️ Account Moderation Notice: {rtype.upper()}", 
                           description=f"A formal system action has been registered against your profile inside **{ctx.guild.name}**.", 
                           color=discord.Color.from_rgb(44, 62, 80))
        dm.add_field(name="📋 Infraction Type", value=rtype, inline=True)
        dm.add_field(name="📋 Stated Reason", value=f"```text\n{reason}\n```", inline=False)
        dm.add_field(name="Case ID", value=f"`{wid}`", inline=True)
        dm.add_field(name="Platform", value=source, inline=True)
        dm.add_field(name="Expiration", value=expiry, inline=True)
        dm.add_field(name="⚖️ Appeal", value=f"[Open Appeal Form]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        try: await member.send(embed=dm)
        except: pass

    # Execution
    if source == "Discord" and member:
        if rtype == "Timeout": await member.timeout(td or datetime.timedelta(days=1), reason=reason)
        elif rtype == "Ban": await ctx.guild.ban(member, reason=reason)

    # Log to Sheet
    append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW", 
                   [uid, name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid])

    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.from_rgb(44, 62, 80))
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target", value=f"<@{uid}>"); log.add_field(name="Case ID", value=f"`{wid}`")
    log.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    await ctx.followup.send(embed=log)
    # ── 7. SLASH COMMANDS ──────────────────────────────────────────────────
@bot.tree.command(name="warn", description="Issue a formal warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", "Discord", "Never")

@bot.tree.command(name="timeout", description="Apply a temporary timeout")
async def timeout(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int, unit: str):
    # unit should be 'minutes', 'hours', or 'days'
    await interaction.response.defer()
    params = {unit: amount}
    td = datetime.timedelta(**params)
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, td)

@bot.tree.command(name="ban", description="Permanently ban a user")
async def ban(interaction: discord.Interaction, target: str, reason: str):
    await interaction.response.defer()
    tid = extract_id(target)
    member = interaction.guild.get_member(int(tid))
    await run_moderation_action(interaction, tid, tid, member, reason, "Ban", "Discord", "Never")

@bot.tree.command(name="setprefix", description="Modify nickname with prefix")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)
    if len(prefix.strip()) > 5: 
        return await interaction.followup.send("❌ Prefix too long (max 5 chars).", ephemeral=True)
    
    # Keep existing name suffix
    parts = interaction.user.display_name.split(" - ")
    base = parts[-1].strip()
    new = f"{prefix.strip()} - {base}"
    try:
        await interaction.user.edit(nick=new[:32])
        await interaction.followup.send(f"✅ Nickname updated to: `{new[:32]}`", ephemeral=True)
    except: await interaction.followup.send("❌ Error: Hierarchy issue.", ephemeral=True)

@bot.tree.command(name="check", description="Check moderation history")
async def check(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    rows = read_all_rows()
    user_warnings = []
    for r in rows:
        r = pad(r)
        if str(r[0]) == str(user.id): user_warnings.append((r, 0))
    
    embed = discord.Embed(title=f"History for {user.name}", color=discord.Color.blue())
    for r, _ in user_warnings[-5:]: # Last 5
        embed.add_field(name=f"Case {r[6]}", value=f"{r[11]} | {r[10]}\n{r[4]}", inline=False)
    await interaction.followup.send(embed=embed)
    # ── 8. VERIFICATION SYSTEM ───────────────────────────────────────────
@bot.tree.command(name="verify", description="Link your Roblox account to your Discord")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # Generate a unique security state for this verification session
    token = str(uuid.uuid4())
    
    # Store token + Discord ID + Time in the "TempStates" sheet
    # This prevents CSRF attacks
    append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET}!A:C:append?valueInputOption=RAW", 
                   [token, str(interaction.user.id), str(int(time.time()))])
    
    # Construct the Roblox OAuth URL
    url = (
        f"https://apis.roblox.com/oauth/v1/authorize?"
        f"client_id={ROBLOX_CLIENT_ID}&"
        f"redirect_uri={ROBLOX_REDIRECT_URI}&"
        f"scope=openid+profile&"
        f"response_type=code&"
        f"state={token}"
    )
    
    embed = discord.Embed(
        title="🔗 Roblox Verification",
        description="Click the link below to authorize your Roblox account. This session will expire in 5 minutes.",
        color=discord.Color.green()
    )
    await interaction.followup.send(embed=embed, content=f"[Click here to Verify]({url})", ephemeral=True)

@bot.tree.command(name="checklink", description="Verify if you are already linked")
async def checklink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for r in rows[1:]:
        if r and r[0].strip() == str(interaction.user.id):
            return await interaction.followup.send(f"✅ You are linked to: **{r[2]}**", ephemeral=True)
    
    await interaction.followup.send("❌ You are not currently linked.", ephemeral=True)

@bot.tree.command(name="forceunlink", description="Staff: Remove a user's link")
async def forceunlink(interaction: discord.Interaction, user: discord.Member):
    # Add your own permission check here (e.g., if interaction.user.guild_permissions.administrator)
    await interaction.response.defer(ephemeral=True)
    
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(user.id):
            update_row(i + 2, VERIFY_SHEET, ["", "", ""])
            return await interaction.followup.send(f"✅ Removed link for {user.mention}.", ephemeral=True)
    
    await interaction.followup.send("❌ No link found for this user.", ephemeral=True)
    # ── 9. FLASK SERVER & CALLBACK ───────────────────────────────────────────
app = Flask(__name__)

def pop_oauth_state_from_cloud(token):
    # Validates and removes the temporary state
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STATE_SHEET}!A:C"
    rows = requests.get(url, headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(token).strip():
            # Check if state is < 5 mins old
            if (int(time.time()) - int(r[2].strip())) > 300: return None
            # Clear state
            update_row(i + 2, STATE_SHEET, ["", "", ""])
            return r[1].strip()
    return None

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state)
    if not did: return "❌ Session expired or invalid state.", 403
    
    try:
        # 1. Exchange OAuth Code for Access Token
        token_resp = requests.post("https://apis.roblox.com/oauth/v1/token", data={
            "client_id": ROBLOX_CLIENT_ID, 
            "client_secret": ROBLOX_CLIENT_SECRET, 
            "grant_type": "authorization_code", 
            "code": code, 
            "redirect_uri": ROBLOX_REDIRECT_URI
        }, timeout=10)
        token_data = token_resp.json()
        
        # 2. Get User Info
        user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", 
                            headers={"Authorization": f"Bearer {token_data['access_token']}"}, timeout=10).json()
        
        # 3. Save to "VerifiedUsers" sheet
        append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C:append?valueInputOption=RAW", 
                       [str(did), str(user['sub']), user['preferred_username']])
        
        # 4. Sync Nickname and DM confirmation in bot thread
        async def sync_rename():
            for g in bot.guilds:
                try:
                    m = await g.fetch_member(int(did))
                    if m: 
                        await m.edit(nick=user['preferred_username'][:32], reason="Auto-Verify Sync")
                        await m.send(f"✅ Successfully linked your Roblox account: **{user['preferred_username']}**")
                except: pass
        bot.loop.create_task(sync_rename())
        return "✅ Success! You are verified. You can close this window."
    except Exception as e: return f"❌ Backend Error: {e}", 500

    # ── 10. BACKGROUND TASKS (EXPIRY SWEEPER) ─────────────────────────────
@tasks.loop(hours=24)
async def expiry_sweeper():
    rows = read_all_rows()
    current_date = datetime.datetime.utcnow().date()
    
    for idx, raw in enumerate(rows):
        r = pad(raw)
        # Skip if already revoked, empty user ID, or 'Never'
        if r[7].upper() == "TRUE" or not r[0] or r[13] in ("Never", "", "None"): 
            continue
            
        try:
            # Parse expiry date from sheet (Assumes format YYYY-MM-DD)
            expiry = datetime.datetime.strptime(r[13].split(" ")[0], "%Y-%m-%d").date()
            
            if current_date >= expiry:
                # Process the unban/un-timeout
                for guild in bot.guilds:
                    try:
                        if r[11] == "Ban": 
                            await guild.unban(discord.Object(id=int(r[0])))
                        elif r[11] == "Timeout":
                            m = await guild.fetch_member(int(r[0]))
                            if m: await m.timeout(None)
                    except: continue
                
                # Update sheet to mark as revoked
                r[7] = "TRUE"
                r[8] = "System Auto-Expiry"
                r[9] = str(datetime.datetime.utcnow())
                update_row(idx + 2, SHEET_NAME, r)
        except Exception:
            continue
            # ── 11. STATUS HEARTBEAT TASK ──────────────────────────────────────────
@tasks.loop(minutes=5)
async def status_task():
    try:
        # Example: Ping an endpoint to ensure the bot service is responding
        # You can add logic here to report health status to a Discord channel
        pass
    except: pass

# ── 12. FINAL STARTUP LOGIC ────────────────────────────────────────────
if __name__ == "__main__":
    @bot.event
    async def on_ready():
        await bot.tree.sync()
        expiry_sweeper.start()
        status_task.start()
        print(f"✅ Bot is fully operational as {bot.user}")

    # Run Discord Bot in a dedicated thread
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    
    # Start Flask Web Server on the Render-assigned port
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Flask Web Server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
