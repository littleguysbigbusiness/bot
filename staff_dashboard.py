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
import datetime
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
    "manage_site": "Edit site content (Home/Careers text)",
    "manage_departments": "Manage departments, members, promotions, and LOA",
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
    if not resp.ok:
        raise RuntimeError(f"publishMessage failed (HTTP {resp.status_code}): {resp.text}")
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
    if not resp.ok:
        raise RuntimeError(f"GetEntry failed (HTTP {resp.status_code}): {resp.text}")
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


# ── Generic sheet-backed record store ──────────────────────────────────────
# Every "table" below (Departments, DepartmentMembers, PromotionRequests,
# DepartmentLog, LOA, ClockSessions) is a Google Sheet tab addressed by these
# helpers: auto-create the tab + header row on first use, read all rows as
# dicts keyed by the header, append a new record, or upsert-by-key. Imports
# from bot are deferred (inside functions) since staff_dashboard is imported
# by bot.py before those names exist at module scope.

_sheet_checked_names = set()


def _col_letter(index_from_1: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA, etc."""
    letters = ""
    n = index_from_1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_range_urls(sheet_name: str, num_cols: int):
    from bot import SPREADSHEET_ID
    last_col = _col_letter(num_cols)
    read_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_name}!A:{last_col}"
    append_url = f"{read_url}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    return read_url, append_url


def _ensure_sheet(sheet_name: str, header_row: list):
    if sheet_name in _sheet_checked_names:
        return
    _sheet_checked_names.add(sheet_name)
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, _ = _sheet_range_urls(sheet_name, len(header_row))
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        if resp.ok:
            return
        requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
            headers=sheets_headers(),
            json={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
            timeout=10,
        )
        last_col = _col_letter(len(header_row))
        header_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_name}!A1:{last_col}1?valueInputOption=RAW"
        requests.put(header_url, headers=sheets_headers(), json={"values": [header_row]}, timeout=10)
        print(f"[StaffDashboard] Created '{sheet_name}' sheet tab")
    except Exception as e:
        print(f"[StaffDashboard] Could not verify/create '{sheet_name}' sheet: {e}")


def _sheet_read_all(sheet_name: str, header_row: list) -> list:
    from bot import sheets_headers
    _ensure_sheet(sheet_name, header_row)
    read_url, _ = _sheet_range_urls(sheet_name, len(header_row))
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
    except Exception as e:
        print(f"[StaffDashboard] {sheet_name} read error: {e}")
        return []
    out = []
    for i, row in enumerate(rows):
        if i == 0:
            continue  # header
        padded = row + [""] * (len(header_row) - len(row))
        out.append({header_row[j]: padded[j] for j in range(len(header_row))})
    return out


def _sheet_append_row(sheet_name: str, header_row: list, record: dict):
    from bot import sheets_headers
    _ensure_sheet(sheet_name, header_row)
    _, append_url = _sheet_range_urls(sheet_name, len(header_row))
    row_values = [str(record.get(h, "")) for h in header_row]
    try:
        requests.post(append_url, headers=sheets_headers(), json={"values": [row_values]}, timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] {sheet_name} append error: {e}")


def _sheet_update_row_by_key(sheet_name: str, header_row: list, key_col: str, key_value: str, record: dict):
    """Overwrites the first row where key_col == key_value, or appends a new one if not found."""
    from bot import sheets_headers, SPREADSHEET_ID
    _ensure_sheet(sheet_name, header_row)
    read_url, append_url = _sheet_range_urls(sheet_name, len(header_row))
    key_idx = header_row.index(key_col)
    row_values = [str(record.get(h, "")) for h in header_row]
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) > key_idx and row[key_idx].strip() == str(key_value).strip():
                sheet_row = i + 1
                last_col = _col_letter(len(header_row))
                range_str = f"{sheet_name}!A{sheet_row}:{last_col}{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"
                requests.put(url, headers=sheets_headers(), json={"values": [row_values]}, timeout=10)
                return
        requests.post(append_url, headers=sheets_headers(), json={"values": [row_values]}, timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] {sheet_name} update error: {e}")


def _sheet_delete_row_by_key(sheet_name: str, header_row: list, key_col: str, key_value: str):
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, _ = _sheet_range_urls(sheet_name, len(header_row))
    key_idx = header_row.index(key_col)
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) > key_idx and row[key_idx].strip() == str(key_value).strip():
                sheet_row = i + 1
                last_col = _col_letter(len(header_row))
                range_str = f"{sheet_name}!A{sheet_row}:{last_col}{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}:clear"
                requests.post(url, headers=sheets_headers(), timeout=10)
                return
    except Exception as e:
        print(f"[StaffDashboard] {sheet_name} delete error: {e}")


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


# ── SiteContent sheet (Key | Value) — editable text shown on the public pages ──

SITE_CONTENT_SHEET = "SiteContent"
_site_sheet_checked = False

DEFAULT_SITE_CONTENT = {
    "weeks_count": "6",
    "bus_driver_status": "Closed",
    "latest_title": "Ryde Buses Now Under Testing",
    "latest_body": "New Managers have been hired. Managers: Unclebob (Bus Driver), Supergoodhelp (Bus Driver), "
                   "Mr.Eual (Bus Marshal) and Levitty (Bus Marshal).",
    "stat_passenger_journeys": "0",
    "stat_buses_in_fleet": "8",
    "stat_employees": "8",
    "stat_depots": "2",
    "our_people": json.dumps([
        {"name": "Awesomebuilderaiden", "title": "CEO",
         "quote": "I love Busways because I get to drive buses and work on the game. I am certain you will enjoy it."},
        {"name": "Unclebob119", "title": "Bus Driver Manager",
         "quote": "Being able to empower our staff with the tools and support they need to do their best is the "
                   "thing I enjoy the most. Every day provides an opportunity to learn."},
    ]),
}


def _site_content_sheet_urls():
    from bot import SPREADSHEET_ID
    read_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SITE_CONTENT_SHEET}!A:B"
    append_url = f"{read_url}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    return read_url, append_url


def _ensure_site_content_sheet():
    global _site_sheet_checked
    if _site_sheet_checked:
        return
    _site_sheet_checked = True
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, _ = _site_content_sheet_urls()
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        if resp.ok:
            return
        requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
            headers=sheets_headers(),
            json={"requests": [{"addSheet": {"properties": {"title": SITE_CONTENT_SHEET}}}]},
            timeout=10,
        )
        header_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SITE_CONTENT_SHEET}!A1:B1?valueInputOption=RAW"
        requests.put(header_url, headers=sheets_headers(), json={"values": [["Key", "Value"]]}, timeout=10)
        print(f"[StaffDashboard] Created '{SITE_CONTENT_SHEET}' sheet tab")
    except Exception as e:
        print(f"[StaffDashboard] Could not verify/create '{SITE_CONTENT_SHEET}' sheet: {e}")


def get_site_content() -> dict:
    from bot import sheets_headers
    _ensure_site_content_sheet()
    read_url, _ = _site_content_sheet_urls()
    values = dict(DEFAULT_SITE_CONTENT)
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for row in rows[1:]:  # skip header
            if len(row) >= 1 and row[0].strip():
                values[row[0].strip()] = row[1] if len(row) >= 2 else ""
    except Exception as e:
        print(f"[StaffDashboard] Site content read error: {e}")
    return values


def _site_content_upsert(key: str, value: str):
    from bot import sheets_headers, SPREADSHEET_ID
    read_url, append_url = _site_content_sheet_urls()
    try:
        resp = requests.get(read_url, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) >= 1 and row[0].strip() == key:
                sheet_row = i + 1
                range_str = f"{SITE_CONTENT_SHEET}!A{sheet_row}:B{sheet_row}"
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"
                requests.put(url, headers=sheets_headers(), json={"values": [[key, value]]}, timeout=10)
                return
        requests.post(append_url, headers=sheets_headers(), json={"values": [[key, value]]}, timeout=10)
    except Exception as e:
        print(f"[StaffDashboard] Site content upsert error: {e}")


def save_site_content(updates: dict):
    _ensure_site_content_sheet()
    for key, value in updates.items():
        _site_content_upsert(key, value)


# ── Departments, membership, promotions, LOA, clock-in ─────────────────────

DEPARTMENTS_SHEET = "Departments"
DEPARTMENTS_HEADERS = ["DeptID", "Name", "Track", "RankOrder", "ResourcesLink", "ChecklistItems", "MinDaysInDept", "DiscordRoleID", "RequireInGameToClockIn"]

DEPT_MEMBERS_SHEET = "DepartmentMembers"
DEPT_MEMBERS_HEADERS = ["MembershipID", "DiscordUserID", "Username", "DeptID", "JoinedDeptDate", "ChecklistProgress"]

PROMOTION_REQUESTS_SHEET = "PromotionRequests"
PROMOTION_REQUESTS_HEADERS = ["RequestID", "DiscordUserID", "Username", "FromDeptID", "ToDeptID", "RequestedAt", "Status", "ReviewedBy", "ReviewedAt"]

DEPARTMENT_LOG_SHEET = "DepartmentLog"
DEPARTMENT_LOG_HEADERS = ["Timestamp", "DiscordUserID", "Action", "DeptID", "PerformedBy", "Details"]

LOA_SHEET = "LOA"
LOA_HEADERS = ["LoaID", "DiscordUserID", "Username", "StartDate", "EndDate", "Reason", "Status", "RequestedAt"]

CLOCK_SESSIONS_SHEET = "ClockSessions"
CLOCK_SESSIONS_HEADERS = ["SessionID", "DiscordUserID", "Username", "DeptID", "ClockInAt", "ClockOutAt", "DurationMinutes", "VerifiedInGame"]


def _to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def log_department_action(discord_user_id, action, dept_id, performed_by, details=""):
    record = {
        "Timestamp": datetime.datetime.utcnow().isoformat(),
        "DiscordUserID": str(discord_user_id),
        "Action": action,
        "DeptID": dept_id,
        "PerformedBy": performed_by,
        "Details": details,
    }
    _sheet_append_row(DEPARTMENT_LOG_SHEET, DEPARTMENT_LOG_HEADERS, record)


def list_department_log(limit=100) -> list:
    rows = _sheet_read_all(DEPARTMENT_LOG_SHEET, DEPARTMENT_LOG_HEADERS)
    return list(reversed(rows))[:limit]


# — Departments —

def list_departments() -> list:
    rows = _sheet_read_all(DEPARTMENTS_SHEET, DEPARTMENTS_HEADERS)
    for r in rows:
        try:
            r["ChecklistItemsList"] = json.loads(r.get("ChecklistItems") or "[]")
        except Exception:
            r["ChecklistItemsList"] = []
    return rows


def get_department(dept_id: str):
    return next((d for d in list_departments() if d["DeptID"] == dept_id), None)


def save_department(dept_id, name, track, rank_order, resources_link, checklist_items, min_days, discord_role_id, require_in_game) -> str:
    if not dept_id:
        dept_id = uuid.uuid4().hex[:10]
    record = {
        "DeptID": dept_id,
        "Name": name,
        "Track": track,
        "RankOrder": str(rank_order),
        "ResourcesLink": resources_link,
        "ChecklistItems": json.dumps(checklist_items),
        "MinDaysInDept": str(min_days),
        "DiscordRoleID": discord_role_id or "",
        "RequireInGameToClockIn": "true" if require_in_game else "false",
    }
    _sheet_update_row_by_key(DEPARTMENTS_SHEET, DEPARTMENTS_HEADERS, "DeptID", dept_id, record)
    return dept_id


def delete_department(dept_id: str):
    _sheet_delete_row_by_key(DEPARTMENTS_SHEET, DEPARTMENTS_HEADERS, "DeptID", dept_id)


def get_next_department(dept: dict):
    """The department in the same Track with the next-higher RankOrder, or None if dept is top of its track."""
    same_track = [d for d in list_departments() if d["Track"] == dept["Track"] and d["DeptID"] != dept["DeptID"]]
    higher = [d for d in same_track if _to_int(d["RankOrder"]) > _to_int(dept["RankOrder"])]
    if not higher:
        return None
    return min(higher, key=lambda d: _to_int(d["RankOrder"]))


# — Department membership —

def list_department_members(dept_id=None, discord_user_id=None) -> list:
    rows = _sheet_read_all(DEPT_MEMBERS_SHEET, DEPT_MEMBERS_HEADERS)
    for r in rows:
        try:
            r["ChecklistProgressDict"] = json.loads(r.get("ChecklistProgress") or "{}")
        except Exception:
            r["ChecklistProgressDict"] = {}
    if dept_id:
        rows = [r for r in rows if r["DeptID"] == dept_id]
    if discord_user_id:
        rows = [r for r in rows if r["DiscordUserID"] == str(discord_user_id)]
    return rows


def add_department_member(discord_user_id, username, dept_id) -> str:
    membership_id = uuid.uuid4().hex[:10]
    record = {
        "MembershipID": membership_id,
        "DiscordUserID": str(discord_user_id),
        "Username": username,
        "DeptID": dept_id,
        "JoinedDeptDate": datetime.datetime.utcnow().isoformat(),
        "ChecklistProgress": "{}",
    }
    _sheet_append_row(DEPT_MEMBERS_SHEET, DEPT_MEMBERS_HEADERS, record)
    log_department_action(discord_user_id, "joined", dept_id, username)
    return membership_id


def remove_department_member(membership_id: str, performed_by: str = ""):
    target = next((m for m in list_department_members() if m["MembershipID"] == membership_id), None)
    _sheet_delete_row_by_key(DEPT_MEMBERS_SHEET, DEPT_MEMBERS_HEADERS, "MembershipID", membership_id)
    if target:
        log_department_action(target["DiscordUserID"], "removed", target["DeptID"], performed_by)


def update_member_checklist(membership_id: str, item: str, done: bool):
    target = next((m for m in list_department_members() if m["MembershipID"] == membership_id), None)
    if not target:
        return
    progress = target.get("ChecklistProgressDict", {})
    progress[item] = bool(done)
    record = {h: target.get(h, "") for h in DEPT_MEMBERS_HEADERS}
    record["ChecklistProgress"] = json.dumps(progress)
    _sheet_update_row_by_key(DEPT_MEMBERS_SHEET, DEPT_MEMBERS_HEADERS, "MembershipID", membership_id, record)


def move_member_department(membership_id: str, new_dept_id: str, performed_by: str = ""):
    target = next((m for m in list_department_members() if m["MembershipID"] == membership_id), None)
    if not target:
        return
    old_dept_id = target["DeptID"]
    record = {h: target.get(h, "") for h in DEPT_MEMBERS_HEADERS}
    record["DeptID"] = new_dept_id
    record["JoinedDeptDate"] = datetime.datetime.utcnow().isoformat()
    record["ChecklistProgress"] = "{}"
    _sheet_update_row_by_key(DEPT_MEMBERS_SHEET, DEPT_MEMBERS_HEADERS, "MembershipID", membership_id, record)
    log_department_action(target["DiscordUserID"], f"promoted:{old_dept_id}->{new_dept_id}", new_dept_id, performed_by)


def get_promotion_eligibility(membership: dict, dept: dict) -> dict:
    next_dept = get_next_department(dept)
    reasons = []
    if next_dept is None:
        reasons.append("No higher department in this track.")

    try:
        joined = datetime.datetime.fromisoformat(membership["JoinedDeptDate"])
        days_in = (datetime.datetime.utcnow() - joined).days
    except Exception:
        days_in = 0
    min_days = _to_int(dept.get("MinDaysInDept"))
    if days_in < min_days:
        reasons.append(f"Needs {min_days - days_in} more day(s) in this department.")

    progress = membership.get("ChecklistProgressDict", {})
    items = dept.get("ChecklistItemsList", [])
    incomplete = [item for item in items if not progress.get(item)]
    if incomplete:
        reasons.append(f"{len(incomplete)} checklist item(s) not yet marked complete.")

    return {
        "eligible": next_dept is not None and not reasons,
        "reasons": reasons,
        "next_dept": next_dept,
        "days_in": days_in,
        "min_days": min_days,
    }


# — Promotion requests —

def list_promotion_requests(status=None) -> list:
    rows = _sheet_read_all(PROMOTION_REQUESTS_SHEET, PROMOTION_REQUESTS_HEADERS)
    if status:
        rows = [r for r in rows if r["Status"] == status]
    return rows


def create_promotion_request(discord_user_id, username, from_dept_id, to_dept_id) -> str:
    request_id = uuid.uuid4().hex[:10]
    record = {
        "RequestID": request_id,
        "DiscordUserID": str(discord_user_id),
        "Username": username,
        "FromDeptID": from_dept_id,
        "ToDeptID": to_dept_id,
        "RequestedAt": datetime.datetime.utcnow().isoformat(),
        "Status": "Pending",
        "ReviewedBy": "",
        "ReviewedAt": "",
    }
    _sheet_append_row(PROMOTION_REQUESTS_SHEET, PROMOTION_REQUESTS_HEADERS, record)
    return request_id


def review_promotion_request(request_id, status, reviewed_by):
    target = next((r for r in list_promotion_requests() if r["RequestID"] == request_id), None)
    if not target:
        return None
    record = {h: target.get(h, "") for h in PROMOTION_REQUESTS_HEADERS}
    record["Status"] = status
    record["ReviewedBy"] = reviewed_by
    record["ReviewedAt"] = datetime.datetime.utcnow().isoformat()
    _sheet_update_row_by_key(PROMOTION_REQUESTS_SHEET, PROMOTION_REQUESTS_HEADERS, "RequestID", request_id, record)
    return target


# — LOA —

def list_loa(status=None, discord_user_id=None) -> list:
    rows = _sheet_read_all(LOA_SHEET, LOA_HEADERS)
    if status:
        rows = [r for r in rows if r["Status"] == status]
    if discord_user_id:
        rows = [r for r in rows if r["DiscordUserID"] == str(discord_user_id)]
    return rows


def create_loa_request(discord_user_id, username, start_date, end_date, reason) -> str:
    loa_id = uuid.uuid4().hex[:10]
    record = {
        "LoaID": loa_id,
        "DiscordUserID": str(discord_user_id),
        "Username": username,
        "StartDate": start_date,
        "EndDate": end_date,
        "Reason": reason,
        "Status": "Pending",
        "RequestedAt": datetime.datetime.utcnow().isoformat(),
    }
    _sheet_append_row(LOA_SHEET, LOA_HEADERS, record)
    return loa_id


def review_loa_request(loa_id, status):
    target = next((r for r in list_loa() if r["LoaID"] == loa_id), None)
    if not target:
        return
    record = {h: target.get(h, "") for h in LOA_HEADERS}
    record["Status"] = status
    _sheet_update_row_by_key(LOA_SHEET, LOA_HEADERS, "LoaID", loa_id, record)


# — Clock in/out —

def get_active_clock_session(discord_user_id, dept_id):
    rows = _sheet_read_all(CLOCK_SESSIONS_SHEET, CLOCK_SESSIONS_HEADERS)
    return next((r for r in rows if r["DiscordUserID"] == str(discord_user_id) and r["DeptID"] == dept_id and not r.get("ClockOutAt")), None)


def clock_in(discord_user_id, username, dept_id, verified_in_game: bool) -> str:
    session_id = uuid.uuid4().hex[:10]
    record = {
        "SessionID": session_id,
        "DiscordUserID": str(discord_user_id),
        "Username": username,
        "DeptID": dept_id,
        "ClockInAt": datetime.datetime.utcnow().isoformat(),
        "ClockOutAt": "",
        "DurationMinutes": "",
        "VerifiedInGame": "true" if verified_in_game else "false",
    }
    _sheet_append_row(CLOCK_SESSIONS_SHEET, CLOCK_SESSIONS_HEADERS, record)
    log_department_action(discord_user_id, "clocked_in", dept_id, username)
    return session_id


def clock_out(session_id, performed_by=""):
    target = next((r for r in _sheet_read_all(CLOCK_SESSIONS_SHEET, CLOCK_SESSIONS_HEADERS) if r["SessionID"] == session_id), None)
    if not target:
        return
    try:
        started = datetime.datetime.fromisoformat(target["ClockInAt"])
        duration = int((datetime.datetime.utcnow() - started).total_seconds() // 60)
    except Exception:
        duration = 0
    record = {h: target.get(h, "") for h in CLOCK_SESSIONS_HEADERS}
    record["ClockOutAt"] = datetime.datetime.utcnow().isoformat()
    record["DurationMinutes"] = str(duration)
    _sheet_update_row_by_key(CLOCK_SESSIONS_SHEET, CLOCK_SESSIONS_HEADERS, "SessionID", session_id, record)
    log_department_action(target["DiscordUserID"], "clocked_out", target["DeptID"], performed_by or target.get("Username", ""))


def list_active_clock_sessions() -> list:
    return [r for r in _sheet_read_all(CLOCK_SESSIONS_SHEET, CLOCK_SESSIONS_HEADERS) if not r.get("ClockOutAt")]


# — Roblox in-game verification (reuses the /verify OAuth mapping bot.py already keeps) —

def get_roblox_user_id_for_discord(discord_user_id: str):
    from bot import sheets_headers, VERIFIED_USERS_READ_URL
    try:
        resp = requests.get(VERIFIED_USERS_READ_URL, headers=sheets_headers(), timeout=10)
        rows = resp.json().get("values", [])
        for row in rows:
            if len(row) >= 2 and row[0].strip() == str(discord_user_id).strip():
                return row[1].strip()
    except Exception as e:
        print(f"[StaffDashboard] VerifiedUsers lookup error: {e}")
    return None


def is_user_in_game(roblox_user_id: str) -> bool:
    try:
        result = run_action_live("list_players")
        if not result.get("ok"):
            return False
        players = result.get("data") or []
        return any(str(p.get("userId")) == str(roblox_user_id) for p in players)
    except Exception as e:
        print(f"[StaffDashboard] in-game check error: {e}")
        return False


# — Discord role sync on promotion approval —

def sync_discord_role(discord_user_id: str, old_role_id: str, new_role_id: str):
    """Best-effort: remove the old dept's role, add the new one. Returns (ok, message)."""
    if not DASHBOARD_GUILD_ID:
        return False, "DASHBOARD_GUILD_ID not configured"
    from bot import bot as bot_instance

    guild = bot_instance.get_guild(int(DASHBOARD_GUILD_ID))
    if guild is None:
        return False, "Bot is not in the configured guild"

    async def _do_sync():
        member = await guild.fetch_member(int(discord_user_id))
        if old_role_id:
            old_role = guild.get_role(int(old_role_id))
            if old_role and old_role in member.roles:
                await member.remove_roles(old_role, reason="Department promotion")
        added_name = None
        if new_role_id:
            new_role = guild.get_role(int(new_role_id))
            if new_role:
                await member.add_roles(new_role, reason="Department promotion")
                added_name = new_role.name
        return added_name

    try:
        future = asyncio.run_coroutine_threadsafe(_do_sync(), bot_instance.loop)
        added_name = future.result(timeout=10)
        return True, (f"Role synced ({added_name})" if added_name else "Role synced")
    except Exception as e:
        return False, f"Role sync failed: {e}"


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
# Busways brand colors, pulled from the live computed styles on
# sites.google.com/view/bwr7r/home: indigo header band (#3F51B5) + orange
# accent used on every button/link/CTA there (#FF9900).

BASE_STYLE = """
<style>
  :root {
    --bg: #ffffff; --bg-alt: #f2f2f2; --border: #dadce0;
    --text: #1f2020; --muted: #5e5e5e;
    --primary: #3f51b5; --primary-dark: #303f9f;
    --accent: #ff9900; --accent-dark: #cc7a00;
    --danger: #d93025; --radius: 8px;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text); margin: 0;
    font-family: "Google Sans", Roboto, -apple-system, "Segoe UI", sans-serif;
  }
  a { color: var(--accent-dark); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 32px; background: var(--primary); color: #fff;
  }
  .nav .brand { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 500; color: #fff; }
  .nav .links { display: flex; align-items: center; gap: 24px; font-size: 14px; }
  .nav .links a { color: rgba(255,255,255,0.85); }
  .nav .links a:hover { color: #fff; text-decoration: none; }
  .nav .who { display: flex; align-items: center; gap: 10px; font-size: 13px; color: rgba(255,255,255,0.85); }
  .nav .who a { color: rgba(255,255,255,0.85); }
  .nav .who a:hover { color: #fff; }
  .nav img.avatar { width: 26px; height: 26px; border-radius: 50%; }
  .btn {
    display: inline-block; background: var(--accent); color: #fff; border: none;
    border-radius: 20px; padding: 9px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
  }
  .btn:hover { background: var(--accent-dark); text-decoration: none; }
  .btn.secondary { background: transparent; color: var(--primary); border: 1px solid var(--border); }
  .btn.secondary:hover { background: var(--bg-alt); }
  .btn.danger { background: var(--danger); }
  .btn.danger:hover { background: #a50e0e; }
  .hero { background: var(--bg-alt); padding: 56px 32px; text-align: center; }
  .hero h1 { font-size: 32px; margin: 0 0 10px; font-weight: 500; color: var(--primary); }
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
  input:focus, select:focus { outline: none; border-color: var(--primary); }
  button:not(.btn) {
    background: var(--bg-alt); color: var(--primary-dark); border: 1px solid var(--border);
    border-radius: 6px; padding: 9px 16px; font-size: 14px; cursor: pointer;
  }
  button:not(.btn):hover { background: #e8eaf6; }
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
        cls = ' style="color:#fff;font-weight:600;"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    links = [link("/staff/", "Home", "home")]
    if _is_logged_in():
        links.append(link("/staff/remote-server-management", "Remote Server Management", "rsm"))
        links.append(link("/staff/my-departments", "My Departments", "my-departments"))
        if _has_permission("manage_site"):
            links.append(link("/staff/site-management", "Site Management", "site"))
        if _has_permission("manage_departments"):
            links.append(link("/staff/departments", "Departments", "departments"))
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
      <a href="/" class="brand" style="text-decoration:none;">🚌 Busways Region 7 Roblox</a>
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


DISCORD_INVITE = "https://discord.gg/Bhwj8MPZ"

# Content below is copied from sites.google.com/view/bwr7r/* (Home, Careers,
# Customer Info, About Us, Drivers Info) so this site is a real replacement,
# not a set of links back out to Google Sites.


def _public_nav(active: str = ""):
    def link(href, label, key):
        style = ' style="color:#fff;font-weight:600;"' if key == active else ""
        return f'<a href="{href}"{style}>{label}</a>'

    links = [
        link("/", "Home", "home"),
        link("/careers", "Careers", "careers"),
        link("/drivers-info", "Drivers Info", "drivers"),
        link("/customer-info", "Customer Info", "customer"),
        link("/about-us", "About Us", "about"),
    ]
    return f"""
    <div class="nav">
      <a href="/" class="brand" style="text-decoration:none;">🚌 Busways Region 7 Roblox</a>
      <div class="links">{''.join(links)}</div>
      <div class="who"><a class="btn" href="/staff/">Staff Portal</a></div>
    </div>
    """


def _public_footer():
    return f"""
    <div class="nav" style="justify-content:center;font-size:13px;flex-direction:column;gap:6px;padding:24px;">
      <div><a href="/about-us" style="color:#fff;">Our company</a> &middot; <a href="{DISCORD_INVITE}" style="color:#fff;">Our networks</a> &middot; <a href="/careers" style="color:#fff;">Careers</a></div>
      <div style="opacity:0.85;">Join us on Discord: <a href="{DISCORD_INVITE}" style="color:#fff;text-decoration:underline;">{DISCORD_INVITE}</a></div>
    </div>
    """


PUBLIC_HOME_HTML = """
<html><head><title>Busways Region 7 Roblox</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<div class="hero">
  <h1>Over {{ content.weeks_count }} Weeks of Connecting people, communities and journeys</h1>
  <a class="btn secondary" href="/about-us">About Us</a>
</div>
<main>
  <div class="card">
    <h2>Become a Driver at Busways</h2>
    <p class="hint">Check out all our current opportunities and apply today!</p>
    <a class="btn" href="/careers">Opportunities</a>
  </div>

  <div class="card">
    <h2>The latest at Busways</h2>
    <p><b>{{ content.latest_title }}</b></p>
    <p class="hint">{{ content.latest_body }}</p>
  </div>

  <div class="grid">
    <div class="card" style="text-align:center;"><h2 style="color:var(--primary);font-size:22px;">{{ content.stat_passenger_journeys }}</h2><p class="hint">Passenger journeys</p></div>
    <div class="card" style="text-align:center;"><h2 style="color:var(--primary);font-size:22px;">{{ content.stat_buses_in_fleet }}</h2><p class="hint">Buses in fleet</p></div>
    <div class="card" style="text-align:center;"><h2 style="color:var(--primary);font-size:22px;">{{ content.stat_employees }}</h2><p class="hint">Employees at Busways</p></div>
    <div class="card" style="text-align:center;"><h2 style="color:var(--primary);font-size:22px;">{{ content.stat_depots }}</h2><p class="hint">Depots owned by Busways</p></div>
  </div>

  <div class="card">
    <h2>Our People</h2>
    {% for person in people %}
    <p{% if not loop.first %} style="margin-top:14px;"{% endif %}><b>{{ person.name }}</b> — {{ person.title }}</p>
    <p class="hint">"{{ person.quote }}"</p>
    {% else %}
    <p class="hint">No team bios added yet.</p>
    {% endfor %}
  </div>
</main>
{{ footer|safe }}
</body></html>
"""


def render_public_home():
    content = get_site_content()
    try:
        people = json.loads(content.get("our_people", "[]"))
    except Exception:
        people = []
    return render_template_string(PUBLIC_HOME_HTML, style=BASE_STYLE, nav=_public_nav("home"), footer=_public_footer(), content=content, people=people)


CAREERS_HTML = """
<html><head><title>Careers — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Careers</h2>
    <p><b>Bus Driver Applications Status: {{ content.bus_driver_status }}</b></p>
    <p class="hint">To apply, join the game or go to the Google Form to apply.</p>
  </div>
</main>
{{ footer|safe }}
</body></html>
"""


def render_careers():
    return render_template_string(CAREERS_HTML, style=BASE_STYLE, nav=_public_nav("careers"), footer=_public_footer(), content=get_site_content())


CUSTOMER_INFO_HTML = """
<html><head><title>Customer Info — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Customer Info</h2>
    <p class="hint">More info in our Discord.</p>
    <a class="btn secondary" href="{{ discord }}">Busways Discord</a>
  </div>
</main>
{{ footer|safe }}
</body></html>
"""


def render_customer_info():
    return render_template_string(CUSTOMER_INFO_HTML, style=BASE_STYLE, nav=_public_nav("customer"), footer=_public_footer(), discord=DISCORD_INVITE)


ABOUT_US_HTML = """
<html><head><title>About Us — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>About Us</h2>
    <p>Hey! We're the team behind Busways Roblox, a group of people who really love buses, cities, and creating cool stuff together on Roblox.</p>
    <p>Busways started as a fun little project between friends. We wanted to make something that felt real, where you could drive around, explore detailed cities, and experience what it's like to run proper bus routes. We had no idea it would grow into such an awesome community.</p>
    <p>Now, Busways is more than just a game. It's a place where people meet, drive, roleplay, and just have a good time. Every update and new feature comes from our team's passion and your amazing feedback.</p>
    <p>We're always learning, improving, and trying to make Busways the best it can be. But what really makes it special is all of you, the players who make the game come alive.</p>
    <p>So from all of us, thank you for being part of the journey.<br>See you out on the road! 🚌🧡🤍</p>
  </div>
  <div class="card">
    <h2>Our expertise</h2>
    <p class="hint">Busways Roblox is a transport solution provider that partners with other Roblox agencies to bring innovative, reliable and flexible services to the Roblox community. We're unencumbered, flexible and fast with a leadership team that is both forward thinking and responsive to the changing nature of the domestic transport industry.</p>
    <ul>
      <li>Public transport</li><li>Bus charters</li><li>Customer experience</li>
      <li>School buses</li><li>Network planning</li><li>Scheduling</li><li>Asset management</li>
    </ul>
  </div>
</main>
{{ footer|safe }}
</body></html>
"""


def render_about_us():
    return render_template_string(ABOUT_US_HTML, style=BASE_STYLE, nav=_public_nav("about"), footer=_public_footer())


DRIVERS_INFO_PUBLIC_HTML = """
<html><head><title>Drivers Info — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Drivers Info</h2>
    <h2 style="font-size:14px;color:var(--muted);font-weight:400;">Our Routes</h2>
    <p class="hint">Route info coming soon. Staff should use the <a href="/staff/remote-server-management">Staff Portal</a> for live operations.</p>
  </div>
</main>
{{ footer|safe }}
</body></html>
"""


def render_drivers_info_public():
    return render_template_string(DRIVERS_INFO_PUBLIC_HTML, style=BASE_STYLE, nav=_public_nav("drivers"), footer=_public_footer())


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
      <h2>Remote Server Management</h2>
      <p class="hint">Live player list, kicks, and announcements for the running server.</p>
      <a class="btn secondary" href="/staff/remote-server-management">Open</a>
    </div>
    {% endif %}
    <div class="card">
      <h2>My Departments</h2>
      <p class="hint">Your department memberships, training checklists, and clock-in.</p>
      <a class="btn secondary" href="/staff/my-departments">Open</a>
    </div>
    {% if can_manage_departments %}
    <div class="card">
      <h2>Departments</h2>
      <p class="hint">Manage departments, members, promotions, and LOA.</p>
      <a class="btn secondary" href="/staff/departments">Open</a>
    </div>
    {% endif %}
    {% if can_manage_site %}
    <div class="card">
      <h2>Site Management</h2>
      <p class="hint">Edit the Home page stats, news, and Careers status.</p>
      <a class="btn secondary" href="/staff/site-management">Open</a>
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
        can_manage_site=_has_permission("manage_site"),
        can_manage_departments=_has_permission("manage_departments"),
        can_manage_permissions=_has_permission(MANAGE_PERMISSIONS),
    )


REMOTE_SERVER_MANAGEMENT_HTML = """
<html><head><title>Remote Server Management — Busways</title>{{ style|safe }}</head>
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


@staff_bp.route("/remote-server-management")
def remote_server_management():
    if not _is_logged_in():
        return redirect("/staff/login")
    can_list_players = _has_permission("list_players")
    can_kick = _has_permission("kick_player")
    can_announce = _has_permission("announce")
    if not (can_list_players or can_kick or can_announce):
        return _error_page(403, "No access", "Your account doesn't have permission to view Remote Server Management. Ask an admin to grant your role access in Permissions Manager.")
    return render_template_string(
        REMOTE_SERVER_MANAGEMENT_HTML,
        style=BASE_STYLE,
        nav=_nav("rsm"),
        can_list_players=can_list_players,
        can_kick=can_kick,
        can_announce=can_announce,
    )


SITE_MANAGEMENT_HTML = """
<html><head><title>Site Management — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Home page</h2>
    <p class="hint">Controls the hero heading, news card, and stats on the public Home page.</p>
    <label>Weeks of connecting people<br><input id="weeksCount" type="number" min="0" value="{{ content.weeks_count }}" style="width:100px;"></label>
    <div style="margin-top:14px;">
      <label>Latest news title<br><input id="latestTitle" value="{{ content.latest_title }}" style="width:100%;"></label>
    </div>
    <div style="margin-top:10px;">
      <label>Latest news body<br><textarea id="latestBody" rows="3" style="width:100%;font-family:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--border);border-radius:6px;">{{ content.latest_body }}</textarea></label>
    </div>
    <div style="margin-top:14px;display:flex;gap:16px;flex-wrap:wrap;">
      <label>Passenger journeys<br><input id="statPassengers" type="number" value="{{ content.stat_passenger_journeys }}" style="width:100px;"></label>
      <label>Buses in fleet<br><input id="statBuses" type="number" value="{{ content.stat_buses_in_fleet }}" style="width:100px;"></label>
      <label>Employees<br><input id="statEmployees" type="number" value="{{ content.stat_employees }}" style="width:100px;"></label>
      <label>Depots<br><input id="statDepots" type="number" value="{{ content.stat_depots }}" style="width:100px;"></label>
    </div>
  </div>

  <div class="card">
    <h2>Careers page</h2>
    <label>Bus Driver Applications status<br>
      <select id="busDriverStatus">
        <option value="Open" {% if content.bus_driver_status == "Open" %}selected{% endif %}>Open</option>
        <option value="Closed" {% if content.bus_driver_status == "Closed" %}selected{% endif %}>Closed</option>
      </select>
    </label>
  </div>

  <div class="card">
    <h2>Our People</h2>
    <p class="hint">Team bios shown on the Home page.</p>
    <div id="peopleRows"></div>
    <button type="button" onclick="addPersonRow()">+ Add person</button>
  </div>

  <button class="btn" onclick="saveSiteContent()">Save changes</button>
  <pre class="output" id="saveResult"></pre>
</main>
<script>
const initialPeople = {{ people|tojson }};

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function personRowHtml(p) {
  p = p || {name:"", title:"", quote:""};
  return `<div class="personRow" style="border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:10px;">
    <input class="pName" placeholder="Name" value="${escapeHtml(p.name)}" style="width:200px;">
    <input class="pTitle" placeholder="Title" value="${escapeHtml(p.title)}" style="width:200px;">
    <button type="button" class="danger" onclick="this.closest('.personRow').remove()">Remove</button><br>
    <textarea class="pQuote" placeholder="Quote" rows="2" style="width:100%;margin-top:6px;font-family:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--border);border-radius:6px;">${escapeHtml(p.quote)}</textarea>
  </div>`;
}

function addPersonRow(p) {
  document.getElementById("peopleRows").insertAdjacentHTML("beforeend", personRowHtml(p));
}

initialPeople.forEach(addPersonRow);

async function saveSiteContent() {
  const people = Array.from(document.querySelectorAll(".personRow")).map(row => ({
    name: row.querySelector(".pName").value,
    title: row.querySelector(".pTitle").value,
    quote: row.querySelector(".pQuote").value,
  }));
  const body = {
    weeks_count: document.getElementById("weeksCount").value,
    latest_title: document.getElementById("latestTitle").value,
    latest_body: document.getElementById("latestBody").value,
    stat_passenger_journeys: document.getElementById("statPassengers").value,
    stat_buses_in_fleet: document.getElementById("statBuses").value,
    stat_employees: document.getElementById("statEmployees").value,
    stat_depots: document.getElementById("statDepots").value,
    bus_driver_status: document.getElementById("busDriverStatus").value,
    people,
  };
  document.getElementById("saveResult").textContent = "Saving...";
  const res = await fetch("/staff/api/site-content", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  document.getElementById("saveResult").textContent = JSON.stringify(await res.json(), null, 2);
}
</script>
</body></html>
"""


@staff_bp.route("/site-management")
def site_management_page():
    guard = _require_permission("manage_site")
    if guard:
        return guard
    content = get_site_content()
    try:
        people = json.loads(content.get("our_people", "[]"))
    except Exception:
        people = []
    return render_template_string(
        SITE_MANAGEMENT_HTML,
        style=BASE_STYLE,
        nav=_nav("site"),
        content=content,
        people=people,
    )


@staff_bp.route("/api/site-content", methods=["POST"])
def api_site_content_save():
    guard = _api_permission_guard("manage_site")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}

    updates = {
        "weeks_count": str(data.get("weeks_count", "")).strip() or "0",
        "bus_driver_status": "Open" if str(data.get("bus_driver_status", "")).strip().lower() == "open" else "Closed",
        "latest_title": str(data.get("latest_title", "")).strip()[:200],
        "latest_body": str(data.get("latest_body", "")).strip()[:1000],
        "stat_passenger_journeys": str(data.get("stat_passenger_journeys", "")).strip() or "0",
        "stat_buses_in_fleet": str(data.get("stat_buses_in_fleet", "")).strip() or "0",
        "stat_employees": str(data.get("stat_employees", "")).strip() or "0",
        "stat_depots": str(data.get("stat_depots", "")).strip() or "0",
    }

    clean_people = []
    for p in (data.get("people") or [])[:20]:
        if isinstance(p, dict) and (p.get("name") or p.get("quote")):
            clean_people.append({
                "name": str(p.get("name", "")).strip()[:80],
                "title": str(p.get("title", "")).strip()[:80],
                "quote": str(p.get("quote", "")).strip()[:500],
            })
    updates["our_people"] = json.dumps(clean_people)

    save_site_content(updates)
    print(f"[StaffDashboard] {session.get('staff_name')} updated site content")
    return jsonify({"ok": True})


# ── Departments admin page ──────────────────────────────────────────────

DEPARTMENTS_ADMIN_HTML = """
<html><head><title>Departments — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  <div class="card">
    <h2>Create / edit a department</h2>
    <input type="hidden" id="deptId">
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
      <label>Name<br><input id="deptName" style="width:200px;"></label>
      <label>Track<br><input id="deptTrack" placeholder="e.g. Bus Driver" style="width:160px;"></label>
      <label>Rank order<br><input id="deptRank" type="number" style="width:90px;"></label>
      <label>Min days in dept<br><input id="deptMinDays" type="number" value="0" style="width:90px;"></label>
      <label>Discord role ID<br><input id="deptRoleId" style="width:160px;"></label>
    </div>
    <label>Resources link<br><input id="deptResources" style="width:100%;"></label>
    <div style="margin-top:10px;">
      <label>Checklist items (one per line)<br>
        <textarea id="deptChecklist" rows="4" style="width:100%;font-family:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--border);border-radius:6px;"></textarea>
      </label>
    </div>
    <label style="display:block;margin-top:10px;"><input type="checkbox" id="deptRequireInGame"> Require in-game verification to clock in</label>
    <div style="margin-top:12px;">
      <button class="btn" onclick="saveDepartment()">Save department</button>
      <button onclick="resetDeptForm()">Clear form</button>
    </div>
    <pre class="output" id="deptSaveResult"></pre>
  </div>

  <div class="card">
    <h2>Departments</h2>
    <table>
      <tr><th>Name</th><th>Track</th><th>Rank</th><th>Min days</th><th>In-game?</th><th></th></tr>
      {% for d in departments %}
      <tr>
        <td>{{ d.Name }}</td><td>{{ d.Track }}</td><td>{{ d.RankOrder }}</td><td>{{ d.MinDaysInDept }}</td>
        <td>{{ "Yes" if d.RequireInGameToClockIn == "true" else "No" }}</td>
        <td>
          <button
            data-id="{{ d.DeptID }}" data-name="{{ d.Name }}" data-track="{{ d.Track }}"
            data-rank="{{ d.RankOrder }}" data-mindays="{{ d.MinDaysInDept }}" data-roleid="{{ d.DiscordRoleID }}"
            data-resources="{{ d.ResourcesLink }}" data-checklist="{{ d.ChecklistItemsList|join('\n') }}"
            data-requireingame="{{ d.RequireInGameToClockIn }}"
            onclick="editDepartment(this.dataset)">Edit</button>
          <button class="danger" onclick="deleteDepartment('{{ d.DeptID }}')">Delete</button>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="empty">No departments yet — create one above.</td></tr>
      {% endfor %}
    </table>
  </div>

  {% for dept in departments %}
  <div class="card">
    <h2>{{ dept.Name }} members</h2>
    <table>
      <tr><th>Username</th><th>Joined</th><th>Checklist</th><th>Move to</th><th></th></tr>
      {% for m in members_by_dept.get(dept.DeptID, []) %}
      <tr>
        <td>{{ m.Username }}</td>
        <td>{{ m.JoinedDeptDate[:10] }}</td>
        <td>
          {% for item in dept.ChecklistItemsList %}
          <label style="display:block;"><input type="checkbox" data-membership="{{ m.MembershipID }}" data-item="{{ item }}" onchange="toggleChecklist(this.dataset.membership, this.dataset.item, this.checked)" {% if m.ChecklistProgressDict.get(item) %}checked{% endif %}> {{ item }}</label>
          {% endfor %}
        </td>
        <td>
          <select id="moveTarget_{{ m.MembershipID }}">
            {% for d2 in departments %}<option value="{{ d2.DeptID }}" {% if d2.DeptID == dept.DeptID %}selected{% endif %}>{{ d2.Name }}</option>{% endfor %}
          </select>
          <button onclick="moveMember('{{ m.MembershipID }}')">Move</button>
        </td>
        <td><button class="danger" onclick="removeMember('{{ m.MembershipID }}')">Remove</button></td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">No members yet.</td></tr>
      {% endfor %}
    </table>
    <div style="margin-top:10px;">
      <select id="addMemberSelect_{{ dept.DeptID }}">
        {% for gm in guild_members %}<option value="{{ gm.id }}" data-name="{{ gm.name }}">{{ gm.name }}</option>{% endfor %}
      </select>
      <button onclick="addMember('{{ dept.DeptID }}')">Add member</button>
    </div>
  </div>
  {% endfor %}

  <div class="card">
    <h2>Pending promotion requests</h2>
    <table>
      <tr><th>Username</th><th>From</th><th>To</th><th>Requested</th><th></th></tr>
      {% for r in promotion_requests %}
      <tr>
        <td>{{ r.Username }}</td><td>{{ dept_names.get(r.FromDeptID, r.FromDeptID) }}</td><td>{{ dept_names.get(r.ToDeptID, r.ToDeptID) }}</td>
        <td>{{ r.RequestedAt[:10] }}</td>
        <td><button onclick="reviewPromotion('{{ r.RequestID }}','Approved')">Approve</button> <button class="danger" onclick="reviewPromotion('{{ r.RequestID }}','Denied')">Deny</button></td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">No pending requests.</td></tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>Pending LOA requests</h2>
    <table>
      <tr><th>Username</th><th>Start</th><th>End</th><th>Reason</th><th></th></tr>
      {% for l in loa_requests %}
      <tr>
        <td>{{ l.Username }}</td><td>{{ l.StartDate }}</td><td>{{ l.EndDate }}</td><td>{{ l.Reason }}</td>
        <td><button onclick="reviewLoa('{{ l.LoaID }}','Approved')">Approve</button> <button class="danger" onclick="reviewLoa('{{ l.LoaID }}','Denied')">Deny</button></td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">No pending LOA requests.</td></tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>Currently clocked in</h2>
    <table>
      <tr><th>Username</th><th>Dept</th><th>Since</th><th>Verified in-game</th><th></th></tr>
      {% for c in active_clock_sessions %}
      <tr>
        <td>{{ c.Username }}</td><td>{{ dept_names.get(c.DeptID, c.DeptID) }}</td><td>{{ c.ClockInAt[:16] }}</td>
        <td>{{ "Yes" if c.VerifiedInGame == "true" else "No" }}</td>
        <td><button class="danger" onclick="forceClockOut('{{ c.SessionID }}')">Force clock out</button></td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="empty">Nobody currently clocked in.</td></tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>Staff profile lookup</h2>
    <p class="hint">Look up any staff member's departments and training checklists, and check off items directly here.</p>
    <input id="lookupId" placeholder="Discord user ID">
    <button onclick="lookupProfile()">Look up</button>
    <div id="lookupResult"></div>
  </div>

  <div class="card">
    <h2>Activity log</h2>
    <table>
      <tr><th>When</th><th>Action</th><th>Dept</th><th>By</th></tr>
      {% for l in activity_log %}
      <tr><td>{{ l.Timestamp[:16] }}</td><td>{{ l.Action }}</td><td>{{ dept_names.get(l.DeptID, l.DeptID) }}</td><td>{{ l.PerformedBy }}</td></tr>
      {% else %}
      <tr><td colspan="4" class="empty">No activity logged yet.</td></tr>
      {% endfor %}
    </table>
  </div>
</main>
<script>
async function call(url, body) {
  const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}) });
  return res.json();
}

function resetDeptForm() {
  document.getElementById("deptId").value = "";
  document.getElementById("deptName").value = "";
  document.getElementById("deptTrack").value = "";
  document.getElementById("deptRank").value = "";
  document.getElementById("deptMinDays").value = "0";
  document.getElementById("deptRoleId").value = "";
  document.getElementById("deptResources").value = "";
  document.getElementById("deptChecklist").value = "";
  document.getElementById("deptRequireInGame").checked = false;
}

function editDepartment(d) {
  // d is a button's dataset (DOMStringMap) — the browser already HTML-decoded
  // each attribute, so these are the raw values, not JSON.
  document.getElementById("deptId").value = d.id;
  document.getElementById("deptName").value = d.name;
  document.getElementById("deptTrack").value = d.track;
  document.getElementById("deptRank").value = d.rank;
  document.getElementById("deptMinDays").value = d.mindays;
  document.getElementById("deptRoleId").value = d.roleid;
  document.getElementById("deptResources").value = d.resources;
  document.getElementById("deptChecklist").value = d.checklist || "";
  document.getElementById("deptRequireInGame").checked = d.requireingame === "true";
  window.scrollTo(0, 0);
}

async function saveDepartment() {
  const body = {
    deptId: document.getElementById("deptId").value,
    name: document.getElementById("deptName").value,
    track: document.getElementById("deptTrack").value,
    rankOrder: document.getElementById("deptRank").value,
    minDays: document.getElementById("deptMinDays").value,
    discordRoleId: document.getElementById("deptRoleId").value,
    resourcesLink: document.getElementById("deptResources").value,
    checklistItems: document.getElementById("deptChecklist").value.split("\\n").map(s => s.trim()).filter(Boolean),
    requireInGame: document.getElementById("deptRequireInGame").checked,
  };
  document.getElementById("deptSaveResult").textContent = "Saving...";
  const data = await call("/staff/api/departments", body);
  document.getElementById("deptSaveResult").textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 500);
}

async function deleteDepartment(id) {
  if (!confirm("Delete this department? This does not remove its members.")) return;
  await call("/staff/api/departments/delete", {deptId: id});
  location.reload();
}

async function toggleChecklist(membershipId, item, checked) {
  await call("/staff/api/departments/members/checklist", {membershipId, item, done: checked});
}

async function addMember(deptId) {
  const sel = document.getElementById("addMemberSelect_" + deptId);
  const discordUserId = sel.value;
  const username = sel.options[sel.selectedIndex].dataset.name;
  await call("/staff/api/departments/members/add", {deptId, discordUserId, username});
  location.reload();
}

async function removeMember(membershipId) {
  if (!confirm("Remove this member from the department?")) return;
  await call("/staff/api/departments/members/remove", {membershipId});
  location.reload();
}

async function moveMember(membershipId) {
  const sel = document.getElementById("moveTarget_" + membershipId);
  await call("/staff/api/departments/members/move", {membershipId, newDeptId: sel.value});
  location.reload();
}

async function reviewPromotion(requestId, status) {
  await call("/staff/api/promotions/review", {requestId, status});
  location.reload();
}

async function reviewLoa(loaId, status) {
  await call("/staff/api/loa/review", {loaId, status});
  location.reload();
}

async function forceClockOut(sessionId) {
  await call("/staff/api/clock/force-out", {sessionId});
  location.reload();
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function lookupProfile() {
  const discordUserId = document.getElementById("lookupId").value;
  const container = document.getElementById("lookupResult");
  container.innerHTML = '<pre class="output">Looking up...</pre>';
  const data = await call("/staff/api/profile-lookup", {discordUserId});

  if (data.error) {
    container.innerHTML = `<pre class="output">${escapeHtml(JSON.stringify(data))}</pre>`;
    return;
  }
  if (!data.departments || data.departments.length === 0) {
    container.innerHTML = `<pre class="output">${escapeHtml(data.username || discordUserId)} is not a member of any department.</pre>`;
    return;
  }

  let html = `<p style="margin-top:12px;"><b>${escapeHtml(data.username || discordUserId)}</b></p>`;
  for (const dep of data.departments) {
    html += `<div style="border:1px solid var(--border);border-radius:6px;padding:12px;margin-top:10px;">
      <p><b>${escapeHtml(dep.department)}</b> — ${dep.daysIn}/${dep.minDays} days in department`
      + (dep.nextDept ? ` &middot; next: ${escapeHtml(dep.nextDept)}` : "") + `</p>`;
    for (const item of dep.checklistItems) {
      const checked = dep.checklistProgress[item] ? "checked" : "";
      html += `<label style="display:block;"><input type="checkbox" data-membership="${dep.membershipId}" data-item="${escapeHtml(item)}" onchange="toggleChecklist(this.dataset.membership, this.dataset.item, this.checked)" ${checked}> ${escapeHtml(item)}</label>`;
    }
    if (!dep.eligibleForPromotion && dep.reasons.length) {
      html += `<p class="hint">Not yet eligible: ${escapeHtml(dep.reasons.join(", "))}</p>`;
    }
    html += `</div>`;
  }
  container.innerHTML = html;
}
</script>
</body></html>
"""


@staff_bp.route("/departments")
def departments_page():
    guard = _require_permission("manage_departments")
    if guard:
        return guard

    from bot import bot as bot_instance
    guild = bot_instance.get_guild(int(DASHBOARD_GUILD_ID)) if DASHBOARD_GUILD_ID else None
    guild_members = sorted(
        ({"id": m.id, "name": str(m)} for m in guild.members),
        key=lambda m: m["name"].lower(),
    ) if guild else []

    departments = list_departments()
    dept_names = {d["DeptID"]: d["Name"] for d in departments}
    members_by_dept = {}
    for d in departments:
        members_by_dept[d["DeptID"]] = list_department_members(dept_id=d["DeptID"])

    return render_template_string(
        DEPARTMENTS_ADMIN_HTML,
        style=BASE_STYLE,
        nav=_nav("departments"),
        departments=departments,
        dept_names=dept_names,
        members_by_dept=members_by_dept,
        guild_members=guild_members,
        promotion_requests=list_promotion_requests(status="Pending"),
        loa_requests=list_loa(status="Pending"),
        active_clock_sessions=list_active_clock_sessions(),
        activity_log=list_department_log(limit=50),
    )


@staff_bp.route("/api/departments", methods=["POST"])
def api_departments_save():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    dept_id = save_department(
        dept_id=str(data.get("deptId", "")).strip(),
        name=str(data.get("name", "")).strip()[:100],
        track=str(data.get("track", "")).strip()[:100],
        rank_order=_to_int(data.get("rankOrder")),
        resources_link=str(data.get("resourcesLink", "")).strip()[:500],
        checklist_items=[str(i).strip()[:200] for i in (data.get("checklistItems") or [])][:30],
        min_days=_to_int(data.get("minDays")),
        discord_role_id=str(data.get("discordRoleId", "")).strip(),
        require_in_game=bool(data.get("requireInGame")),
    )
    print(f"[StaffDashboard] {session.get('staff_name')} saved department {dept_id}")
    return jsonify({"ok": True, "deptId": dept_id})


@staff_bp.route("/api/departments/delete", methods=["POST"])
def api_departments_delete():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    dept_id = str(data.get("deptId", "")).strip()
    if not dept_id:
        return jsonify({"error": "deptId is required"}), 400
    delete_department(dept_id)
    return jsonify({"ok": True})


@staff_bp.route("/api/departments/members/add", methods=["POST"])
def api_department_member_add():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    dept_id = str(data.get("deptId", "")).strip()
    discord_user_id = str(data.get("discordUserId", "")).strip()
    username = str(data.get("username", "")).strip()
    if not dept_id or not discord_user_id:
        return jsonify({"error": "deptId and discordUserId are required"}), 400
    membership_id = add_department_member(discord_user_id, username, dept_id)
    return jsonify({"ok": True, "membershipId": membership_id})


@staff_bp.route("/api/departments/members/remove", methods=["POST"])
def api_department_member_remove():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    if not membership_id:
        return jsonify({"error": "membershipId is required"}), 400
    remove_department_member(membership_id, performed_by=session.get("staff_name", ""))
    return jsonify({"ok": True})


@staff_bp.route("/api/departments/members/checklist", methods=["POST"])
def api_department_member_checklist():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    item = str(data.get("item", "")).strip()
    if not membership_id or not item:
        return jsonify({"error": "membershipId and item are required"}), 400
    update_member_checklist(membership_id, item, bool(data.get("done")))
    return jsonify({"ok": True})


@staff_bp.route("/api/departments/members/move", methods=["POST"])
def api_department_member_move():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    new_dept_id = str(data.get("newDeptId", "")).strip()
    if not membership_id or not new_dept_id:
        return jsonify({"error": "membershipId and newDeptId are required"}), 400

    member = next((m for m in list_department_members() if m["MembershipID"] == membership_id), None)
    if not member:
        return jsonify({"error": "membership not found"}), 404
    old_dept = get_department(member["DeptID"])
    new_dept = get_department(new_dept_id)

    move_member_department(membership_id, new_dept_id, performed_by=session.get("staff_name", ""))
    role_ok, role_msg = sync_discord_role(
        member["DiscordUserID"],
        old_dept.get("DiscordRoleID") if old_dept else "",
        new_dept.get("DiscordRoleID") if new_dept else "",
    )
    return jsonify({"ok": True, "roleSync": role_msg})


@staff_bp.route("/api/promotions/review", methods=["POST"])
def api_promotion_review():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    request_id = str(data.get("requestId", "")).strip()
    status = "Approved" if str(data.get("status", "")).strip() == "Approved" else "Denied"
    reviewer = session.get("staff_name", "")

    target = review_promotion_request(request_id, status, reviewer)
    if not target:
        return jsonify({"error": "request not found"}), 404

    role_msg = None
    if status == "Approved":
        member = next(
            (m for m in list_department_members(discord_user_id=target["DiscordUserID"]) if m["DeptID"] == target["FromDeptID"]),
            None,
        )
        if member:
            old_dept = get_department(target["FromDeptID"])
            new_dept = get_department(target["ToDeptID"])
            move_member_department(member["MembershipID"], target["ToDeptID"], performed_by=reviewer)
            _, role_msg = sync_discord_role(
                target["DiscordUserID"],
                old_dept.get("DiscordRoleID") if old_dept else "",
                new_dept.get("DiscordRoleID") if new_dept else "",
            )
    return jsonify({"ok": True, "roleSync": role_msg})


@staff_bp.route("/api/loa/review", methods=["POST"])
def api_loa_review():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    loa_id = str(data.get("loaId", "")).strip()
    status = "Approved" if str(data.get("status", "")).strip() == "Approved" else "Denied"
    review_loa_request(loa_id, status)
    return jsonify({"ok": True})


@staff_bp.route("/api/clock/force-out", methods=["POST"])
def api_clock_force_out():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    session_id = str(data.get("sessionId", "")).strip()
    if not session_id:
        return jsonify({"error": "sessionId is required"}), 400
    clock_out(session_id, performed_by=session.get("staff_name", ""))
    return jsonify({"ok": True})


@staff_bp.route("/api/profile-lookup", methods=["POST"])
def api_profile_lookup():
    guard = _api_permission_guard("manage_departments")
    if guard:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    discord_user_id = str(data.get("discordUserId", "")).strip()
    if not discord_user_id:
        return jsonify({"error": "discordUserId is required"}), 400

    memberships = list_department_members(discord_user_id=discord_user_id)
    departments_by_id = {d["DeptID"]: d for d in list_departments()}
    username = memberships[0]["Username"] if memberships else ""
    profile = []
    for m in memberships:
        dept = departments_by_id.get(m["DeptID"])
        if not dept:
            continue
        elig = get_promotion_eligibility(m, dept)
        profile.append({
            "membershipId": m["MembershipID"],
            "deptId": dept["DeptID"],
            "department": dept["Name"],
            "joinedDeptDate": m["JoinedDeptDate"],
            "daysIn": elig["days_in"],
            "minDays": elig["min_days"],
            "checklistItems": dept.get("ChecklistItemsList", []),
            "checklistProgress": m["ChecklistProgressDict"],
            "eligibleForPromotion": elig["eligible"],
            "reasons": elig["reasons"],
            "nextDept": elig["next_dept"]["Name"] if elig["next_dept"] else None,
        })

    return jsonify({
        "discordUserId": discord_user_id,
        "username": username,
        "departments": profile,
        "loaHistory": list_loa(discord_user_id=discord_user_id),
    })


# ── My Departments (staff-facing, not permission-gated) ────────────────────

def _require_own_membership(membership_id: str):
    """Returns the membership dict if it belongs to the current session user, else None."""
    discord_id = session.get("staff_discord_id")
    m = next((x for x in list_department_members() if x["MembershipID"] == membership_id), None)
    if m and m["DiscordUserID"] == discord_id:
        return m
    return None


MY_DEPARTMENTS_HTML = """
<html><head><title>My Departments — Busways</title>{{ style|safe }}</head>
<body>
{{ nav|safe }}
<main>
  {% for row in rows %}
  <div class="card">
    <h2>{{ row.dept.Name }}</h2>
    <p class="hint">Track: {{ row.dept.Track }} &middot; Rank {{ row.dept.RankOrder }}</p>
    {% if row.dept.ResourcesLink %}<p><a href="{{ row.dept.ResourcesLink }}" target="_blank" rel="noopener">Resources</a></p>{% endif %}

    <p><b>Time in department:</b> {{ row.eligibility.days_in }} / {{ row.eligibility.min_days }} days</p>

    {% if row.dept.ChecklistItemsList %}
    <p><b>Training checklist</b> (checked off by staff, view-only):</p>
    <ul>
      {% for item in row.dept.ChecklistItemsList %}
      <li>{{ "✅" if row.membership.ChecklistProgressDict.get(item) else "⬜" }} {{ item }}</li>
      {% endfor %}
    </ul>
    {% endif %}

    {% if row.eligibility.next_dept %}
      {% if row.eligibility.eligible %}
        {% if row.has_pending_request %}
        <p class="hint">Promotion request to {{ row.eligibility.next_dept.Name }} is pending admin approval.</p>
        {% else %}
        <button class="btn" onclick="requestPromotion('{{ row.membership.MembershipID }}')">Request promotion to {{ row.eligibility.next_dept.Name }}</button>
        {% endif %}
      {% else %}
      <p class="hint">Not yet eligible for {{ row.eligibility.next_dept.Name }}: {{ row.eligibility.reasons|join(", ") }}</p>
      {% endif %}
    {% endif %}

    <div style="margin-top:14px;">
      {% if row.active_session %}
      <button class="danger" onclick="clockOut('{{ row.membership.MembershipID }}')">Clock out</button>
      <span class="hint"> Clocked in since {{ row.active_session.ClockInAt[:16] }}</span>
      {% else %}
      <button onclick="clockIn('{{ row.membership.MembershipID }}')">Clock in{% if row.dept.RequireInGameToClockIn == "true" %} (requires in-game){% endif %}</button>
      {% endif %}
    </div>
    <pre class="output" id="result_{{ row.membership.MembershipID }}"></pre>
  </div>
  {% else %}
  <div class="card"><p class="empty">You're not currently a member of any department.</p></div>
  {% endfor %}

  <div class="card">
    <h2>Leave of Absence</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;">
      <label>Start date<br><input id="loaStart" type="date"></label>
      <label>End date<br><input id="loaEnd" type="date"></label>
      <label>Reason<br><input id="loaReason" style="width:260px;"></label>
    </div>
    <button class="btn" style="margin-top:10px;" onclick="requestLoa()">Request LOA</button>
    <pre class="output" id="loaResult"></pre>

    <table style="margin-top:14px;">
      <tr><th>Start</th><th>End</th><th>Reason</th><th>Status</th></tr>
      {% for l in my_loa %}
      <tr><td>{{ l.StartDate }}</td><td>{{ l.EndDate }}</td><td>{{ l.Reason }}</td><td>{{ l.Status }}</td></tr>
      {% else %}
      <tr><td colspan="4" class="empty">No LOA history.</td></tr>
      {% endfor %}
    </table>
  </div>
</main>
<script>
async function call(url, body) {
  const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}) });
  return res.json();
}
async function requestPromotion(membershipId) {
  const el = document.getElementById("result_" + membershipId);
  el.textContent = "Submitting...";
  const data = await call("/staff/api/my-departments/request-promotion", {membershipId});
  el.textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 800);
}
async function clockIn(membershipId) {
  const el = document.getElementById("result_" + membershipId);
  el.textContent = "Working...";
  const data = await call("/staff/api/my-departments/clock-in", {membershipId});
  el.textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 500);
}
async function clockOut(membershipId) {
  const el = document.getElementById("result_" + membershipId);
  el.textContent = "Working...";
  const data = await call("/staff/api/my-departments/clock-out", {membershipId});
  el.textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 500);
}
async function requestLoa() {
  const startDate = document.getElementById("loaStart").value;
  const endDate = document.getElementById("loaEnd").value;
  const reason = document.getElementById("loaReason").value;
  document.getElementById("loaResult").textContent = "Submitting...";
  const data = await call("/staff/api/my-departments/request-loa", {startDate, endDate, reason});
  document.getElementById("loaResult").textContent = JSON.stringify(data, null, 2);
  if (data.ok) setTimeout(() => location.reload(), 800);
}
</script>
</body></html>
"""


@staff_bp.route("/my-departments")
def my_departments_page():
    if not _is_logged_in():
        return redirect("/staff/login")
    discord_id = session.get("staff_discord_id")
    memberships = list_department_members(discord_user_id=discord_id)
    departments_by_id = {d["DeptID"]: d for d in list_departments()}
    pending_requests = list_promotion_requests(status="Pending")

    rows = []
    for m in memberships:
        dept = departments_by_id.get(m["DeptID"])
        if not dept:
            continue
        elig = get_promotion_eligibility(m, dept)
        has_pending = any(r["DiscordUserID"] == discord_id and r["FromDeptID"] == dept["DeptID"] for r in pending_requests)
        rows.append({
            "membership": m,
            "dept": dept,
            "eligibility": elig,
            "has_pending_request": has_pending,
            "active_session": get_active_clock_session(discord_id, dept["DeptID"]),
        })

    return render_template_string(
        MY_DEPARTMENTS_HTML,
        style=BASE_STYLE,
        nav=_nav("my-departments"),
        rows=rows,
        my_loa=list_loa(discord_user_id=discord_id),
    )


@staff_bp.route("/api/my-departments/request-promotion", methods=["POST"])
def api_request_promotion():
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    member = _require_own_membership(membership_id)
    if not member:
        return jsonify({"error": "membership not found"}), 404
    dept = get_department(member["DeptID"])
    if not dept:
        return jsonify({"error": "department not found"}), 404
    elig = get_promotion_eligibility(member, dept)
    if not elig["eligible"]:
        return jsonify({"error": "not eligible", "reasons": elig["reasons"]}), 400
    existing = [
        r for r in list_promotion_requests(status="Pending")
        if r["DiscordUserID"] == member["DiscordUserID"] and r["FromDeptID"] == dept["DeptID"]
    ]
    if existing:
        return jsonify({"error": "a promotion request is already pending"}), 400
    request_id = create_promotion_request(
        member["DiscordUserID"], session.get("staff_name", ""), dept["DeptID"], elig["next_dept"]["DeptID"]
    )
    return jsonify({"ok": True, "requestId": request_id})


@staff_bp.route("/api/my-departments/clock-in", methods=["POST"])
def api_my_clock_in():
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    member = _require_own_membership(membership_id)
    if not member:
        return jsonify({"error": "membership not found"}), 404
    dept = get_department(member["DeptID"])
    if not dept:
        return jsonify({"error": "department not found"}), 404
    if get_active_clock_session(member["DiscordUserID"], dept["DeptID"]):
        return jsonify({"error": "already clocked in"}), 400

    verified = False
    if dept.get("RequireInGameToClockIn") == "true":
        roblox_id = get_roblox_user_id_for_discord(member["DiscordUserID"])
        if not roblox_id:
            return jsonify({"error": "Link your Roblox account first (use /verify in Discord)."}), 400
        if not is_user_in_game(roblox_id):
            return jsonify({"error": "You must be connected to the live game to clock in for this department."}), 400
        verified = True

    session_id = clock_in(member["DiscordUserID"], session.get("staff_name", ""), dept["DeptID"], verified)
    return jsonify({"ok": True, "sessionId": session_id})


@staff_bp.route("/api/my-departments/clock-out", methods=["POST"])
def api_my_clock_out():
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    membership_id = str(data.get("membershipId", "")).strip()
    member = _require_own_membership(membership_id)
    if not member:
        return jsonify({"error": "membership not found"}), 404
    active = get_active_clock_session(member["DiscordUserID"], member["DeptID"])
    if not active:
        return jsonify({"error": "not currently clocked in"}), 400
    clock_out(active["SessionID"], performed_by=session.get("staff_name", ""))
    return jsonify({"ok": True})


@staff_bp.route("/api/my-departments/request-loa", methods=["POST"])
def api_request_loa():
    if not _is_logged_in():
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(force=True, silent=True) or {}
    start_date = str(data.get("startDate", "")).strip()
    end_date = str(data.get("endDate", "")).strip()
    reason = str(data.get("reason", "")).strip()[:500]
    if not start_date or not end_date:
        return jsonify({"error": "startDate and endDate are required"}), 400
    discord_id = session.get("staff_discord_id")
    loa_id = create_loa_request(discord_id, session.get("staff_name", ""), start_date, end_date, reason)
    return jsonify({"ok": True, "loaId": loa_id})


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
