import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, uuid, datetime, requests, threading, json, re, asyncio, time
from flask import Flask, request, render_template_string
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
SPREADSHEET_ID = "1JXMNLNhJjO55KYBeuec4PrEJPFcZUVJQen0XIoJikb8"
SHEET_NAME, VERIFY_SHEET, STATE_SHEET = "Violations", "VerifiedUsers", "TempStates"
ROBLOX_CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
ROBLOX_CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")
ROBLOX_REDIRECT_URI = "https://bot-h57e.onrender.com/roblox_callback"
GOOGLE_APPEAL_FORM_URL = "https://forms.gle/xCRB3RHfEu6YvhhP8"

# ── Google Auth & Database Helpers ─────────────────────────────────────────
creds = None
if os.path.exists("service_account.json"):
    creds = service_account.Credentials.from_service_account_file("service_account.json", scopes=['https://www.googleapis.com/auth/spreadsheets'])

def get_headers():
    token = os.environ.get('GOOGLE_TOKEN', '')
    if creds:
        if not creds.valid: creds.refresh(Request())
        token = creds.token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def pad(row, length=15): return list(row) + [""] * (length - len(row))
def extract_id(s): return re.search(r'\d+', s).group(0) if re.search(r'\d+', s) else s.strip()

def append_to_sheet(url, row): requests.post(url, headers=get_headers(), json={"values": [row]}, timeout=10)
def update_row(idx, sheet, row):
    rng = f"{sheet}!A{idx}:O{idx}" if sheet == SHEET_NAME else f"{sheet}!A{idx}:C{idx}"
    requests.put(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}?valueInputOption=RAW", 
                 headers=get_headers(), json={"values": [row]}, timeout=10)

def find_warning_by_id(wid):
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O", headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        r = pad(r)
        if r[6].strip().upper() == wid.strip().upper(): return r, i + 2
    return None, None

def get_verified_id(did):
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for r in rows[1:]:
        if r and r[0].strip() == str(did).strip(): return r[1].strip()
    return None

# ── Moderation Engine & History ──────────────────────────────────────────────
async def run_moderation_action(ctx, uid, name, member, reason, rtype, source, expiry, td=None):
    wid = str(uuid.uuid4())[:8].upper()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # 1. Rich Embed DM
    if member:
        dm = discord.Embed(title=f"⚠️ Notice: {rtype.upper()}", description=reason, color=discord.Color.from_rgb(44, 62, 80))
        dm.add_field(name="Case ID", value=f"`{wid}`"); dm.add_field(name="Platform", value=source)
        dm.add_field(name="Expiration", value=expiry)
        dm.add_field(name="Appeal", value=f"[Open Form]({GOOGLE_APPEAL_FORM_URL})", inline=False)
        try: await member.send(embed=dm)
        except: pass

    # 2. Server Action
    if source == "Discord" and member:
        if rtype == "Timeout": await member.timeout(td or datetime.timedelta(days=1), reason=reason)
        elif rtype == "Ban": await ctx.guild.ban(member, reason=reason)

    # 3. Log to Sheet
    append_to_sheet(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:O:append?valueInputOption=RAW", 
                   [uid, name, str(ctx.user), str(ctx.user.id), reason, ts, wid, "FALSE", "", "", source, rtype, ts[:10], expiry, wid])

    log = discord.Embed(title=f"🛑 Logged: {rtype}", color=discord.Color.from_rgb(44, 62, 80))
    if member: log.set_thumbnail(url=member.display_avatar.url)
    log.add_field(name="Target", value=f"<@{uid}>"); log.add_field(name="Case ID", value=f"`{wid}`")
    log.add_field(name="Reason", value=f"```text\n{reason}\n```", inline=False)
    await ctx.followup.send(embed=log)

def build_log_embed(title, warnings, thumb=None):
    embed = discord.Embed(title=title, color=discord.Color.from_rgb(44, 62, 80))
    if thumb: embed.set_thumbnail(url=thumb)
    act, rev = "", ""
    for r, _ in warnings:
        block = f"Case {r[6]} | {r[11]} | {r[10]}\nReason: {r[4]}\nExpires: {r[13]}\n"
        if r[7].upper() == "TRUE": rev += block + f"❌ Revoked by: {r[8]}\n\n"
        else: act += block + "\n"
    embed.add_field(name="⚠️ Active", value=f"```text\n{act.strip() or 'None'}\n```", inline=False)
    embed.add_field(name="✅ Archive", value=f"```text\n{rev.strip() or 'None'}\n```", inline=False)
    return embed

# ── Slash Commands ──────────────────────────────────────────────────────────
@bot.tree.command(name="setprefix", description="Modify nickname with prefix")
async def setprefix(interaction: discord.Interaction, prefix: str):
    await interaction.response.defer(ephemeral=True)
    clean = prefix.strip()
    if not clean or len(clean) > 5 or re.sub(r'[^A-Za-z0-9]', '', clean).upper() in ("CEO", "VCEO"):
        return await interaction.followup.send("❌ Invalid/Restricted prefix.", ephemeral=True)
    
    base = interaction.user.display_name.split(" - ")[-1].strip()
    new = f"{clean} - {base}"
    try:
        await interaction.user.edit(nick=new[:32])
        await interaction.followup.send(f"✅ Nickname: `{new[:32]}`", ephemeral=True)
    except: await interaction.followup.send("❌ Error: Check hierarchy.", ephemeral=True)

@bot.tree.command(name="warn")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str, source: app_commands.Choice[str] = None):
    await interaction.response.defer()
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Warning", source.value if source else "Discord", "Never")

@bot.tree.command(name="timeout")
async def timeout(interaction: discord.Interaction, user: discord.Member, reason: str, amount: int, unit: app_commands.Choice[str]):
    await interaction.response.defer()
    td = datetime.timedelta(**{unit.value: amount})
    exp = (datetime.datetime.utcnow() + td).strftime("%Y-%m-%d %H:%M:%S UTC")
    await run_moderation_action(interaction, str(user.id), str(user), user, reason, "Timeout", "Discord", exp, td)

@bot.tree.command(name="ban")
async def ban(interaction: discord.Interaction, target: str, reason: str):
    await interaction.response.defer()
    tid = extract_id(target)
    await run_moderation_action(interaction, tid, tid, interaction.guild.get_member(int(tid)), reason, "Ban", "Discord", "Never")

@bot.tree.command(name="checklink")
async def checklink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    r = get_verified_id(interaction.user.id)
    await interaction.followup.send(f"✅ Linked: `{r}`" if r else "❌ Not linked.", ephemeral=True)

@bot.tree.command(name="unlink")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(interaction.user.id):
            update_row(i + 2, VERIFY_SHEET, ["", "", ""])
            return await interaction.followup.send("✅ Success.", ephemeral=True)
    await interaction.followup.send("❌ No link found.", ephemeral=True)

@bot.tree.command(name="forceunlink")
async def forceunlink(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction): return
    await interaction.response.defer(ephemeral=True)
    rows = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C", headers=get_headers()).json().get("values", [])
    for i, r in enumerate(rows[1:]):
        if r and r[0].strip() == str(user.id):
            update_row(i + 2, VERIFY_SHEET, ["", "", ""])
            return await interaction.followup.send(f"✅ Removed link for {user.mention}.", ephemeral=True)
    await interaction.followup.send("❌ Not linked.", ephemeral=True)

# ── Background Tasks ──────────────────────────────────────────────────────────
@tasks.loop(hours=24)
async def expiry_sweeper():
    rows = read_all_rows()
    if not rows: return
    current_date = datetime.datetime.utcnow().date()
    for idx, raw in enumerate(rows):
        r = pad(raw)
        if r[COL_REVOKED].upper() == "TRUE" or not r[COL_USER_ID] or r[COL_END_DATE] in ("Never", ""): continue
        try:
            expiry = datetime.datetime.strptime(r[COL_END_DATE].split(" ")[0], "%Y-%m-%d").date()
            if current_date >= expiry:
                for guild in bot.guilds:
                    try:
                        if r[COL_RESTRICTION] == "Ban": await guild.unban(discord.Object(id=int(r[COL_USER_ID])))
                        elif r[COL_RESTRICTION] == "Timeout":
                            m = await guild.fetch_member(int(r[COL_USER_ID]))
                            if m: await m.timeout(None)
                    except: pass
                r[COL_REVOKED] = "TRUE"
                r[COL_REVOKED_BY] = "Auto-Expiry"
                r[COL_REVOKED_AT] = str(datetime.datetime.utcnow())
                update_row(idx + 2, SHEET_NAME, r)
        except: continue

@tasks.loop(seconds=60)
async def status_task():
    try:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if channel:
            resp = requests.get(STATUS_PAGE_URL, timeout=10)
            if resp.status_code == 200:
                # Add your custom embed logic here if needed
                pass
    except: pass

# ── Flask Server & Callback ──────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/roblox_callback')
def roblox_callback():
    code, state = request.args.get("code"), request.args.get("state")
    did = pop_oauth_state_from_cloud(state) # Assumes you have this defined
    if not did: return "❌ Session expired.", 403
    
    try:
        token = requests.post("https://apis.roblox.com/oauth/v1/token", data={
            "client_id": ROBLOX_CLIENT_ID, 
            "client_secret": ROBLOX_CLIENT_SECRET, 
            "grant_type": "authorization_code", 
            "code": code, 
            "redirect_uri": ROBLOX_REDIRECT_URI
        }, timeout=10).json().get("access_token")
        
        user = requests.get("https://apis.roblox.com/oauth/v1/userinfo", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        
        # Log to Sheet
        requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{VERIFY_SHEET}!A:C:append?valueInputOption=RAW", 
                      headers=get_headers(), json={"values": [[str(did), str(user['sub']), user['preferred_username']]]})
        
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
        return "✅ Success!"
    except Exception as e: return f"❌ Error: {e}", 500

# ── Startup Logic ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start bot and flask server together
    threading.Thread(target=lambda: bot.run(DISCORD_TOKEN), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
