"""Staff web dashboard for Busways Region | 7.

Auth: Discord OAuth2 (identify scope). Any member of DASHBOARD_GUILD_ID can
log in; what they can actually do is governed by per-role permissions stored
in the "StaffPermissions" Google Sheet tab (RoleID | RoleName | Permissions),
editable by admins from /staff/permissions. True Discord `administrator`
permission on the guild always grants every permission, including
manage_permissions itself — that's a hard floor so the permission system
can't lock everyone out of configuring itself.

Command delivery: curated actions only (never raw code) are published to the
live game over Roblox Open Cloud MessagingService; results are read back from
a DataStore entry the in-game ClaudeRelay script writes. See
game-scripts/ClaudeRelay.server.lua in the Roblox MCP project for the
in-game side and the full curated action list.
"""

import os
import time
import uuid
import json
import html
import secrets
import asyncio
import urllib.parse
import requests
from flask import Blueprint, request, redirect, session, jsonify, render_template_string

staff_bp = Blueprint("staff", __name__, url_prefix="/staff")

DISCORD_API = "https://discord.com/api"

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DASHBOARD_GUILD_ID = (
    os.environ.get("DASHBOARD_GUILD_ID")
    or os.environ.get("STAFF_GUILD_ID")
    or "1426723848379699350"
)
STAFF_CALLBACK_URL = os.environ.get("STAFF_CALLBACK_URL", "https://bot-h57e.onrender.com/staff/callback")

ROBLOX_OPEN_CLOUD_KEY = os.environ.get("ROBLOX_OPEN_CLOUD_KEY", "")
ROBLOX_UNIVERSE_ID = os.environ.get("ROBLOX_UNIVERSE_ID", "8938366983")

MESSAGING_TOPIC = "ClaudeCommands"
RESULTS_DATASTORE = "ClaudeRelayResults"

# Single source of truth for grantable permissions. Add a new (key, label)
# pair here, plus a route + action handler, to extend the dashboard later.
KNOWN_PERMISSIONS = {
    "list_players": "View player list",
    "kick_player": "Kick a player",
    "announce": "Send an announcement",
}
# Reserved: never grantable via the roles sheet, only ever held by a true
# Discord Administrator on the guild. Prevents the permission system from
# being able to lock admins out of configuring itself.
MANAGE_PERMISSIONS = "manage_permissions"


# ── Open Cloud relay (mirrors game-scripts/ClaudeRelay.server.lua) ─────────

def _publish_action(action: str, params: dict = None) -> str:
    command_id = f"staff_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    message = {"id": command_id, "action": action, "params": params or {}}
    resp = requests.post(
        f"https://apis.roblox.com/cloud/v2/universes/{ROBLOX_UNIVERSE_ID}:publishMessage",
        headers={"x-api-key": ROBLOX_OPEN_CLOUD_KEY, "Content-Type": "application/json"},
        json={"topic": MESSAGING_TOPIC, "message": json.dumps(message)},
        timeout=10,
    )
    resp.raise_for_status()
    return command_id


def _get_result(command_id: str):
    resp = requests.get(
        f"https://apis.roblox.com/datastores/v1/universes/{ROBLOX_UNIVERSE_ID}/standard-datastores/datastore/entries/entry",
        headers={"x-api-key": ROBLOX_OPEN_CLOUD_KEY},
        params={"datastoreName": RESULTS_DATASTORE, "entryKey": command_id},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def run_action_live(action: str, params: dict = None, timeout_s: float = 20.0):
    if not ROBLOX_OPEN_CLOUD_KEY:
        raise RuntimeError("ROBLOX_OPEN_CLOUD_KEY is not set on this server.")
    command_id = _publish_action(action, params)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        entry = _get_result(command_id)
        if entry is not None:
            return entry
    raise TimeoutError("Timed out waiting for the live game to respond. Is a server instance online?")


# ── StaffPermissions sheet (RoleID | RoleName | Permissions) ───────────────
# Uses the same spreadsheet/service-account credentials bot.py already sets
# up. Imports from bot are deferred (inside functions) since staff_dashboard
# is imported by bot.py before those names exist at module scope.

STAFF_PERMISSIONS_SHEET = "StaffPermissions"
_sheet_checked = False


def _sheet_urls():
    from bot import SPREADSHEET_ID
    read_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STAFF_PERMISSIONS_SHEET}!A:C"
    append_url = f"{read_url}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    return read_url, append_url


def _ensure_sheet_exists():
    global _sheet_checked
    if _sheet_checked:
        return
    _sheet_checked = True
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, _ = _sheet_urls()
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        if resp.ok:
            return
        requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
            headers=sheets_headers(),
            json={"requests": [{"addSheet": {"properties": {"title": STAFF_PERMISSIONS_SHEET}}}]},
            timeout=10,
        )
        header_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{STAFF_PERMISSIONS_SHEET}!A1:C1?valueInputOption=RAW"
        requests.put(header_url, headers=sheets_headers(), json={"values": [["RoleID", "RoleName", "Permissions"]]}, timeout=10)
        print(f"[StaffDashboard] Created '{STAFF_PERMISSIONS_SHEET}' sheet tab")
    except Exception as e:
        print(f"[StaffDashboard] Could not verify/create '{STAFF_PERMISSIONS_SHEET}' sheet: {e}")


def _permissions_read():
    from bot import sheets_headers
    _ensure_sheet_exists()
    read_url, _ = _sheet_urls()
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
    except Exception as e:
        print(f"[StaffDashboard] Permissions read error: {e}")
        return []
    out = []
    for row in rows[1:]:  # skip header
        if len(row) >= 2 and row[0].strip():
            perms = [p.strip() for p in row[2].split(",") if p.strip()] if len(row) >= 3 else []
            out.append({"role_id": row[0].strip(), "role_name": row[1].strip(), "permissions": perms})
    return out


def _permissions_upsert(role_id: str, role_name: str, permissions: list):
    from bot import sheets_headers, SPREADSHEET_ID
    _ensure_sheet_exists()
    read_url, append_url = _sheet_urls()
    perms_str = ",".join(p for p in permissions if p in KNOWN_PERMISSIONS)
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) >= 1 and row[0].strip() == str(role_id).strip():
                sheet_row = i + 1
                range_str = f"{STAFF_PERMISSIONS_SHEET}!A{sheet_row}:C{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"
                requests.put(url, headers=sheets_headers(), json={"values": [[role_id, role_name, perms_str]]}, timeout=10)
                return
        requests.post(append_url, headers=sheets_headers(), json={"values": [[role_id, role_name, perms_str]]}, timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] Permissions upsert error: {e}")


def _permissions_delete(role_id: str):
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, _ = _sheet_urls()
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) >= 1 and row[0].strip() == str(role_id).strip():
                sheet_row = i + 1
                range_str = f"{STAFF_PERMISSIONS_SHEET}!A{sheet_row}:C{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}:clear"
                requests.post(url, headers=sheets_headers(), timeout=10)
                return
    except Exception as e:
        print(f"[StaffDashboard] Permissions delete error: {e}")


def _compute_permissions(member) -> set:
    if member.guild_permissions.administrator:
        return set(KNOWN_PERMISSIONS.keys()) | {MANAGE_PERMISSIONS}
    role_ids = {str(r.id) for r in member.roles}
    granted = set()
    for row in _permissions_read():
        if row["role_id"] in role_ids:
            granted.update(p for p in row["permissions"] if p in KNOWN_PERMISSIONS)
    return granted


# ── Auth ─────────────────────────────────────────────────────────────────

def _is_logged_in() -> bool:
    return bool(session.get("staff_discord_id"))


def _has_permission(action: str) -> bool:
    return action in session.get("staff_permissions", [])


def _require_permission(action: str):
    """Returns a Flask response to short-circuit with if unauthorized, else None."""
    if not _is_logged_in():
        return redirect("/staff/login")
    if not _has_permission(action):
        return _error_page(403, "No access", "You don't have permission to do that. Ask an admin to grant your role access in Permissions Manager.")
    return None


def _api_permission_guard(action: str):
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    if not _has_permission(action):
        return jsonify({"error": "forbidden", "missingPermission": action}), 403
    return None


@staff_bp.route("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return _error_page(500, "Not configured", "Missing DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET on the server.")
    state = secrets.token_urlsafe(24)
    session["staff_oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": STAFF_CALLBACK_URL,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return redirect(f"{DISCORD_API}/oauth2/authorize?{urllib.parse.urlencode(params)}")


@staff_bp.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != session.pop("staff_oauth_state", None):
        return _error_page(400, "Login expired", "That login attempt was invalid or expired. Go back and try logging in again.")

    token_resp = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": STAFF_CALLBACK_URL,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if not token_resp.ok:
        print(f"[StaffDashboard] Token exchange failed: {token_resp.status_code} {token_resp.text}")
        return _error_page(502, "Login failed", "Discord login failed during token exchange. Try again.")
    access_token = token_resp.json().get("access_token")

    user_resp = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not user_resp.ok:
        return _error_page(502, "Login failed", "Discord login failed while fetching your account info. Try again.")
    user = user_resp.json()
    discord_id = user.get("id")
    username = user.get("username", "unknown")
    avatar_hash = user.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
        if avatar_hash else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    if not DASHBOARD_GUILD_ID:
        return _error_page(500, "Not configured", "Missing DASHBOARD_GUILD_ID on the server.")

    # Deferred import: staff_dashboard is imported by bot.py before `bot` is
    # constructed, so importing it at module load time would be circular.
    from bot import bot as bot_instance

    guild = bot_instance.get_guild(int(DASHBOARD_GUILD_ID))
    if guild is None:
        return _error_page(500, "Bot offline", "The bot isn't currently connected to the configured Discord server. Try again in a minute.")

    try:
        future = asyncio.run_coroutine_threadsafe(guild.fetch_member(int(discord_id)), bot_instance.loop)
        member = future.result(timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] Member fetch failed for {discord_id}: {e}")
        member = None

    if member is None:
        return _error_page(403, "Not a member", "You are not a member of the Busways Discord server.")

    permissions = _compute_permissions(member)
    if not permissions:
        return _error_page(
            403, "No access",
            f"Signed in as <b>{html.escape(username)}</b>, but your roles don't have any dashboard "
            "permissions yet. Ask an admin to grant your role access in Permissions Manager, then log in again.",
        )

    session["staff_discord_id"] = discord_id
    session["staff_name"] = username
    session["staff_avatar"] = avatar_url
    session["staff_permissions"] = sorted(permissions)
    print(f"[StaffDashboard] {username} ({discord_id}) logged in with permissions: {sorted(permissions)}")
    return redirect("/staff/")


@staff_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/staff/login")


# ── UI ───────────────────────────────────────────────────────────────────
# Styled to echo sites.google.com/view/bwr7r/home — white background, blue
# accent, top nav with wordmark + links, card sections.

BASE_STYLE = """
<style>
  :root {
    --bg: #ffffff; --bg-alt: #f8f9fa; --border: #dadce0;
    --text: #202124; --muted: #5f6368; --accent: #1a73e8; --accent-dark: #174ea6;
    --danger: #d93025; --radius: 8px;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text); margin: 0;
    font-family: "Google Sans", Roboto, -apple-system, "Segoe UI", sans-serif;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 32px; border-bottom: 1px solid var(--border); background: var(--bg);
  }
  .nav .brand { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 500; color: var(--text); }
  .nav .links { display: flex; align-items: center; gap: 24px; font-size: 14px; }
  .nav .links a { color: var(--muted); }
  .nav .links a:hover { color: var(--text); text-decoration: none; }
  .nav .who { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--muted); }
  .nav img.avatar { width: 26px; height: 26px; border-radius: 50%; }
  .btn {
    display: inline-block; background: var(--accent); color: #fff; border: none;
    border-radius: 20px; padding: 9px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
  }
  .btn:hover { background: var(--accent-dark); text-decoration: none; }
  .btn.secondary { background: transparent; color: var(--accent); border: 1px solid var(--border); }
  .btn.secondary:hover { background: var(--bg-alt); }
  .btn.danger { background: var(--danger); }
  .btn.danger:hover { background: #a50e0e; }
  .hero { background: var(--bg-alt); padding: 56px 32px; text-align: center; }
  .hero h1 { font-size: 32px; margin: 0 0 10px; font-weight: 500; }
  .hero p { color: var(--muted); font-size: 16px; margin: 0 0 24px; }
  main { max-width: 760px; margin: 0 auto; padding: 32px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 8px; }
  .card {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 22px 24px; margin-bottom: 18px;
  }
  .card h2 { margin: 0 0 4px; font-size: 16px; font-weight: 500; }
  .card p.hint { color: var(--muted); font-size: 13px; margin: 0 0 14px; }
  input, select {
    background: #fff; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 9px 11px; font-size: 14px; margin: 4px 6px 4px 0;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  button:not(.btn) {
    background: var(--bg-alt); color: var(--accent-dark); border: 1px solid var(--border);
    border-radius: 6px; padding: 9px 16px; font-size: 14px; cursor: pointer;
  }
  button:not(.btn):hover { background: #eef2fc; }
  button.danger { color: var(--danger); border-color: #f3c6c2; }
  button.danger:hover { background: #fdecea; }
  pre.output {
    background: var(--bg-alt); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; font-size: 13px; white-space: pre-wrap; word-break: break-word;
    margin-top: 12px; max-height: 260px; overflow: auto; color: #3c4043;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .empty { color: var(--muted); font-size: 13px; text-align: center; padding: 30px; }
  .checks label { display: inline-flex; align-items: center; gap: 6px; margin-right: 16px; font-size: 13px; color: var(--muted); }
  .errorbox { max-width: 480px; margin: 80px auto; text-align: center; padding: 0 24px; }
  .errorbox .code { font-size: 13px; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; }
  .errorbox h1 { font-size: 24px; margin: 8px 0 14px; }
  .errorbox p { color: var(--muted); font-size: 14px; line-height: 1.6; }
</style>
"""


def _nav(active: str = ""):
    def link(href, label, key):
        cls = ' style="color:var(--text);font-weight:500;"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    links = [link("/staff/", "Home", "home")]
    if _is_logged_in():
        links.append(link("/staff/drivers-info", "Drivers Info", "drivers"))
        if _has_permission(MANAGE_PERMISSIONS):
            links.append(link("/staff/permissions", "Permissions Manager", "permissions"))

    if _is_logged_in():
        name = html.escape(session.get("staff_name", "?"))
        avatar = html.escape(session.get("staff_avatar", ""))
        who = f'<img class="avatar" src="{avatar}"><span>{name}</span><a href="/staff/logout">Log out</a>'
    else:
        who = '<a class="btn" href="/staff/login">Login with Discord</a>'

    return f"""
    <div class="nav">
      <div class="brand">🚌 Busways Region 7 Roblox</div>
      <div class="links">{''.join(links)}</div>
      <div class="who">{who}</div>
    </div>
    """


def _error_page(code: int, title: str, message: str):
    body = f"""
    <html><head><title>{title} — Busways</title>{BASE_STYLE}</head>
    <body>
    {_nav()}
    <div class="errorbox">
      <div class="code">Error {code}</div>
      <h1>{html.escape(title)}</h1>
      <p>{message}</p>
      <p><a href="/staff/">Back to home</a></p>
    </div>
    </body></html>
    """
    return body, code


LANDING_HTML = """
<html><head><title>Busways Region 7 — Staff Portal</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<div class="hero">
  <h1>Busways Region | 7 Staff Portal</h1>
  <p>Manage drivers, routes, and live game operations — no more unguessable links.</p>
  {% if not logged_in %}
  <a class="btn" href="/staff/login">Login with Discord</a>
  {% endif %}
</div>
<main>
  {% if logged_in %}
  <div class="grid">
    {% if can_drivers %}
    <div class="card">
      <h2>Drivers Info</h2>
      <p class="hint">Live player list, kicks, and announcements for the running server.</p>
      <a class="btn secondary" href="/staff/drivers-info">Open</a>
    </div>
    {% endif %}
    {% if can_manage_permissions %}
    <div class="card">
      <h2>Permissions Manager</h2>
      <p class="hint">Grant Discord roles access to dashboard actions.</p>
      <a class="btn secondary" href="/staff/permissions">Open</a>
    </div>
    {% endif %}
  </div>
  {% endif %}
</main>
</body></html>
"""


@staff_bp.route("/")
def landing():
    return render_template_string(
        LANDING_HTML,
        style=BASE_STYLE,
        nav=_nav("home"),
        logged_in=_is_logged_in(),
        can_drivers=_has_permission("list_players") or _has_permission("kick_player") or _has_permission("announce"),
        can_manage_permissions=_has_permission(MANAGE_PERMISSIONS),
    )


DRIVERS_INFO_HTML = """
<html><head><title>Drivers Info — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  {% if can_list_players %}
  <div class="card">
    <h2>Players</h2>
    <p class="hint">Live player list for the currently running server instance(s).</p>
    <button onclick="listPlayers()">Refresh player list</button>
    <pre class="output" id="players"></pre>
  </div>
  {% endif %}

  {% if can_kick %}
  <div class="card">
    <h2>Kick a player</h2>
    <p class="hint">Removes a player from the server instance they're currently in.</p>
    <input id="kickName" placeholder="Player name">
    <input id="kickReason" placeholder="Reason (optional)">
    <button onclick="kickPlayer()">Kick</button>
    <pre class="output" id="kickResult"></pre>
  </div>
  {% endif %}

  {% if can_announce %}
  <div class="card">
    <h2>Announce</h2>
    <p class="hint">Broadcasts a message to all connected clients.</p>
    <input id="announceMsg" placeholder="Message to broadcast" style="width:70%;">
    <button onclick="announce()">Send</button>
    <pre class="output" id="announceResult"></pre>
  </div>
  {% endif %}
</main>
<script>
async function call(url, body) {
  const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}) });
  return res.json();
}
async function listPlayers() {
  document.getElementById("players").textContent = "Loading...";
  document.getElementById("players").textContent = JSON.stringify(await call("/staff/api/players"), null, 2);
}
async function kickPlayer() {
  const player = document.getElementById("kickName").value;
  const reason = document.getElementById("kickReason").value;
  document.getElementById("kickResult").textContent = "Working...";
  document.getElementById("kickResult").textContent = JSON.stringify(await call("/staff/api/kick", {player, reason}), null, 2);
}
async function announce() {
  const message = document.getElementById("announceMsg").value;
  document.getElementById("announceResult").textContent = "Working...";
  document.getElementById("announceResult").textContent = JSON.stringify(await call("/staff/api/announce", {message}), null, 2);
}
</script>
</body></html>
"""


@staff_bp.route("/drivers-info")
def drivers_info():
    if not _is_logged_in():
        return redirect("/staff/login")
    can_list_players = _has_permission("list_players")
    can_kick = _has_permission("kick_player")
    can_announce = _has_permission("announce")
    if not (can_list_players or can_kick or can_announce):
        return _error_page(403, "No access", "Your account doesn't have permission to view Drivers Info. Ask an admin to grant your role access in Permissions Manager.")
    return render_template_string(
        DRIVERS_INFO_HTML,
        style=BASE_STYLE,
        nav=_nav("drivers"),
        can_list_players=can_list_players,
        can_kick=can_kick,
        can_announce=can_announce,
    )


PERMISSIONS_HTML = """
<html><head><title>Permissions Manager</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Grant a role permissions</h2>
    <p class="hint">Anyone with Discord Administrator on this server always has every permission, regardless of what's set here.</p>
    <select id="roleSelect">
      {% for r in guild_roles %}<option value="{{ r.id }}" data-name="{{ r.name }}">{{ r.name }}</option>{% endfor %}
    </select>
    <div class="checks" style="margin-top:10px;">
      {% for key, label in known_permissions.items() %}
      <label><input type="checkbox" class="permCheck" value="{{ key }}"> {{ label }}</label>
      {% endfor %}
    </div>
    <button style="margin-top:10px;" onclick="savePermissions()">Save</button>
    <pre class="output" id="saveResult"></pre>
  </div>

  <div class="card">
    <h2>Current role permissions</h2>
    <table>
      <tr><th>Role</th><th>Permissions</th><th></th></tr>
      {% for row in rows %}
      <tr>
        <td>{{ row.role_name }}</td>
        <td>{{ row.permissions|join(", ") if row.permissions else "—" }}</td>
        <td><button class="danger" onclick="deleteRole('{{ row.role_id }}')">Remove</button></td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="empty">No roles configured yet.</td></tr>
      {% endfor %}
    </table>
  </div>
</main>
<script>
async function call(url, body) {
  const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}) });
  return res.json();
}
async function savePermissions() {
  const sel = document.getElementById("roleSelect");
  const roleId = sel.value;
  const roleName = sel.options[sel.selectedIndex].dataset.name;
  const perms = Array.from(document.querySelectorAll(".permCheck:checked")).map(c => c.value);
  document.getElementById("saveResult").textContent = "Saving...";
  const data = await call("/staff/api/permissions", {roleId, roleName, permissions: perms});
  document.getElementById("saveResult").textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 600);
}
async function deleteRole(roleId) {
  await call("/staff/api/permissions/delete", {roleId});
  location.reload();
}
</script>
</body></html>
"""


@staff_bp.route("/permissions")
def permissions_page():
    guard = _require_permission(MANAGE_PERMISSIONS)
    if guard:
        return guard

    from bot import bot as bot_instance
    guild = bot_instance.get_guild(int(DASHBOARD_GUILD_ID)) if DASHBOARD_GUILD_ID else None
    guild_roles = sorted(
        ({"id": r.id, "name": r.name} for r in guild.roles if not r.is_default()),
        key=lambda r: r["name"].lower(),
    ) if guild else []

    return render_template_string(
        PERMISSIONS_HTML,
        style=BASE_STYLE,
        nav=_nav("permissions"),
        guild_roles=guild_roles,
        known_permissions=KNOWN_PERMISSIONS,
        rows=_permissions_read(),
    )


@staff_bp.route("/api/permissions", methods=["POST"])
def api_permissions_save():
    guard = _api_permission_guard(MANAGE_PERMISSIONS)
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    role_id = str(data.get("roleId", "")).strip()
    role_name = str(data.get("roleName", "")).strip()
    permissions = data.get("permissions") or []
    if not role_id or not role_name:
        return jsonify({"error": "roleId and roleName are required"}), 400
    _permissions_upsert(role_id, role_name, permissions)
    print(f"[StaffDashboard] {session.get('staff_name')} set permissions for role {role_name} ({role_id}): {permissions}")
    return jsonify({"ok": True})


@staff_bp.route("/api/permissions/delete", methods=["POST"])
def api_permissions_delete():
    guard = _api_permission_guard(MANAGE_PERMISSIONS)
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    role_id = str(data.get("roleId", "")).strip()
    if not role_id:
        return jsonify({"error": "roleId is required"}), 400
    _permissions_delete(role_id)
    print(f"[StaffDashboard] {session.get('staff_name')} removed permissions for role {role_id}")
    return jsonify({"ok": True})


# ── Game actions ─────────────────────────────────────────────────────────

@staff_bp.route("/api/players", methods=["POST"])
def api_players():
    guard = _api_permission_guard("list_players")
    if guard:
        return guard
    try:
        return jsonify(run_action_live("list_players"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@staff_bp.route("/api/kick", methods=["POST"])
def api_kick():
    guard = _api_permission_guard("kick_player")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    player = str(data.get("player", "")).strip()
    if not player:
        return jsonify({"error": "player is required"}), 400
    reason = str(data.get("reason", "")).strip()
    try:
        result = run_action_live("kick_player", {"player": player, "reason": reason})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    print(f"[StaffDashboard] {session.get('staff_name')} kicked '{player}': {reason}")
    return jsonify(result)


@staff_bp.route("/api/announce", methods=["POST"])
def api_announce():
    guard = _api_permission_guard("announce")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    try:
        result = run_action_live("announce", {"message": message})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    print(f"[StaffDashboard] {session.get('staff_name')} announced: {message}")
    return jsonify(result)
