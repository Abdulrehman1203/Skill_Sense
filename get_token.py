"""
Developer helper script to print a fresh, valid Clerk JWT token for Swagger UI testing.
Automatically copies the clean token directly to your Windows clipboard!

Usage:
    python get_token.py
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import subprocess
import sys
import dotenv
import jwt
import requests

dotenv.load_dotenv()

clerk_secret = os.getenv("CLERK_SECRET_KEY")
if not clerk_secret:
    print("Error: CLERK_SECRET_KEY is missing from .env file.")
    sys.exit(1)

headers = {
    "Authorization": f"Bearer {clerk_secret}",
    "Content-Type": "application/json",
}

# 1. Fetch recent users from Clerk
users_res = requests.get("https://api.clerk.com/v1/users?limit=10", headers=headers)
if users_res.status_code != 200:
    print(f"Failed to fetch users from Clerk: {users_res.status_code} {users_res.text}")
    sys.exit(1)

users = users_res.json()
if not users:
    print("No users found in your Clerk instance.")
    sys.exit(1)

print("\n--- Available Clerk Users ---")
active_user_candidates = []

for idx, user in enumerate(users):
    email = user["email_addresses"][0]["email_address"] if user.get("email_addresses") else "No email"
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    name = f"{first} {last}".strip() or email
    user_id = user["id"]

    # Check active sessions for this user
    sessions_res = requests.get(f"https://api.clerk.com/v1/sessions?user_id={user_id}", headers=headers)
    sessions = sessions_res.json() if sessions_res.status_code == 200 else []
    
    active_sess = None
    if isinstance(sessions, list):
        for sess in sessions:
            if sess.get("status") == "active":
                active_sess = sess
                break

    status_str = "ACTIVE SESSION" if active_sess else "no active session"
    print(f"[{idx + 1}] {name} (ID: {user_id}) -> {status_str}")
    
    if active_sess:
        active_user_candidates.append((user, active_sess))

if not active_user_candidates:
    print("\nNo active sessions found. Falling back to first user in list.")
    target_user = users[0]
    target_session = None
else:
    # Prefer active user session
    target_user, target_session = active_user_candidates[0]

user_id = target_user["id"]

# 2. Get JWT token
jwt_token = None
if target_session:
    sess_id = target_session["id"]
    token_res = requests.post(f"https://api.clerk.com/v1/sessions/{sess_id}/tokens", headers=headers)
    if token_res.status_code == 200:
        jwt_token = token_res.json().get("jwt")

if not jwt_token:
    print("\nCould not generate session token automatically. Please sign in via frontend once.")
    sys.exit(1)

clean_token = jwt_token.strip()

# Calculate expiration
try:
    payload = jwt.decode(clean_token, options={"verify_signature": False})
    exp_ts = payload.get("exp")
    if exp_ts:
        exp_time = datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
        ttl = max(0, int(exp_ts - datetime.now(timezone.utc).timestamp()))
        exp_info = f"{ttl} seconds (Backend DEBUG mode allows UNLIMITED testing window!)"
    else:
        exp_info = "60 seconds"
except Exception:
    exp_info = "60 seconds"

# Copy automatically to Windows clipboard
try:
    process = subprocess.Popen("clip", stdin=subprocess.PIPE, shell=True)
    process.communicate(input=clean_token.encode("utf-8"))
    clipboard_copied = True
except Exception:
    clipboard_copied = False

print("\n=======================================================")
print(f"Generated Token for User: {target_user.get('first_name', '')} ({user_id})")
print(f"Token Validity / Expiry:  {exp_info}")
if clipboard_copied:
    print("SUCCESS! Token has been AUTOMATICALLY COPIED to your clipboard!")
else:
    print("SUCCESS! Here is your fresh Developer JWT Token:")
print("=======================================================")
print(clean_token)
print("=======================================================")
print("\nInstructions:")
print("1. Go to Swagger UI (http://127.0.0.1:8000/api/docs/)")
print("2. Click 'Authorize'")
print("3. Press Ctrl+V (Paste) into the Value box & click Authorize!\n")
