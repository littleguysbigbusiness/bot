"""Staff web dashboard for Busways Region | 7.

Auth: Discord OAuth2 (identify scope). The authenticated Discord user must be
a member of STAFF_GUILD_ID with administrator/manage_guild/moderate_members —
the same bar as is_admin() in bot.py, just checked outside a slash-command
Interaction.

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
import secrets
import asyncio
import urllib.parse
import requests
from flask import Blueprint, request, redirect, session, jsonify, render_template_string

staff_bp = Blueprint("staff", __name__, url_prefix="/staff")

DISCORD_API = "https://discord.com/api"

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
STAFF_GUILD_ID = os.environ.get("STAFF_GUILD_ID", "")
STAFF_CALLBACK_URL = os.environ.get("STAFF_CALLBACK_URL", "https://bot-h57e.onrender.com/staff/callback")

ROBLOX_OPEN_CLOUD_KEY = os.environ.get("ROBLOX_OPEN_CLOUD_KEY", "")
ROBLOX_UNIVERSE_ID = os.environ.get("ROBLOX_UNIVERSE_ID", "8938366983")

MESSAGING_TOPIC = "ClaudeCommands"
RESULTS_DATASTORE = "ClaudeRelayResults"


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


# ── Auth ─────────────────────────────────────────────────────────────────

def _is_logged_in() -> bool:
    return bool(session.get("staff_discord_id"))


def _require_staff():
    """Returns a redirect response if not authorized, else None."""
    if not _is_logged_in():
        return redirect("/staff/login")
    return None


def _api_guard():
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    return None


@staff_bp.route("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return "Staff dashboard is not configured (missing DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET).", 500
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
        return "Invalid or expired login attempt. Go back and try /staff/login again.", 400

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
        return "Discord login failed (token exchange).", 502
    access_token = token_resp.json().get("access_token")

    user_resp = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not user_resp.ok:
        return "Discord login failed (user fetch).", 502
    user = user_resp.json()
    discord_id = user.get("id")
    username = user.get("username", "unknown")

    if not STAFF_GUILD_ID:
        return "Staff dashboard is not configured (missing STAFF_GUILD_ID).", 500

    # Deferred import: staff_dashboard is imported by bot.py before `bot` is
    # constructed, so importing it at module load time would be circular.
    from bot import bot as bot_instance

    guild = bot_instance.get_guild(int(STAFF_GUILD_ID))
    if guild is None:
        return "Bot is not currently in the configured staff guild.", 500

    try:
        future = asyncio.run_coroutine_threadsafe(guild.fetch_member(int(discord_id)), bot_instance.loop)
        member = future.result(timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] Member fetch failed for {discord_id}: {e}")
        member = None

    if member is None:
        return "You are not a member of the staff Discord server.", 403

    is_staff = (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or member.guild_permissions.moderate_members
    )
    if not is_staff:
        return "Your Discord account doesn't have staff permissions in the server.", 403

    session["staff_discord_id"] = discord_id
    session["staff_name"] = username
    print(f"[StaffDashboard] {username} ({discord_id}) logged in")
    return redirect("/staff/")


@staff_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/staff/login")


DASHBOARD_HTML = """
<html>
<head><title>Busways Staff Control</title></head>
<body style="background:#111;color:#eee;font-family:sans-serif;padding:40px;max-width:700px;margin:auto;">
  <h1>Busways Region | 7 — Staff Control</h1>
  <p>Logged in as <b>{{ name }}</b> — <a href="/staff/logout" style="color:#888;">log out</a></p>
  <hr style="border-color:#333;">

  <h2>Players</h2>
  <button onclick="listPlayers()">Refresh player list</button>
  <pre id="players" style="background:#000;padding:12px;border-radius:6px;white-space:pre-wrap;"></pre>

  <h2>Kick a player</h2>
  <input id="kickName" placeholder="Player name">
  <input id="kickReason" placeholder="Reason (optional)">
  <button onclick="kickPlayer()">Kick</button>
  <pre id="kickResult" style="background:#000;padding:12px;border-radius:6px;white-space:pre-wrap;"></pre>

  <h2>Announce</h2>
  <input id="announceMsg" placeholder="Message to broadcast" style="width:400px;">
  <button onclick="announce()">Send</button>
  <pre id="announceResult" style="background:#000;padding:12px;border-radius:6px;white-space:pre-wrap;"></pre>

<script>
async function call(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
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
</body>
</html>
"""


@staff_bp.route("/")
def dashboard():
    guard = _require_staff()
    if guard:
        return guard
    return render_template_string(DASHBOARD_HTML, name=session.get("staff_name", "?"))


@staff_bp.route("/api/players", methods=["POST"])
def api_players():
    guard = _api_guard()
    if guard:
        return guard
    try:
        return jsonify(run_action_live("list_players"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@staff_bp.route("/api/kick", methods=["POST"])
def api_kick():
    guard = _api_guard()
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
    guard = _api_guard()
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
