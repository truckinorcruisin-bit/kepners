"""
yahoo_setup.py
Direct OAuth2 handshake for the Yahoo Fantasy Sports API.

Rewritten to NOT depend on the `yahoo_oauth` package -- that library never sends a
`scope` parameter, which only worked under Yahoo's OLD app-creation flow where you
pre-selected "Fantasy Sports" as a checkbox on the app itself. Yahoo's current
"Create Application" form has no such checkbox; the scope must instead be requested
at authorization time via `scope=fspt-r`. This script does that explicitly.

SETUP (one-time):
1. Register an app at https://developer.yahoo.com/apps/create/
   - OAuth Client Type: Confidential Client
   - Redirect URI(s): https://localhost:8000
   - API Permissions: leave everything unchecked (Fantasy Sports isn't listed here anymore)
2. Set env vars (or edit the constants below):
     export YAHOO_CLIENT_ID="..."
     export YAHOO_CLIENT_SECRET="..."
3. Run:  python yahoo_setup.py
   - Prints an authorize URL. Open it in a browser, log into Yahoo, click Agree.
   - Yahoo redirects to https://localhost:8000/?code=XXXX -- nothing is actually
     listening there, so the browser will show a connection error page. That's fine --
     copy the FULL resulting URL from the address bar and paste it back into the
     terminal when prompted (or just the code= value, either works).
   - Token is cached in yahoo_token.json and auto-refreshes silently after that.

RUNNING THIS IN GITHUB ACTIONS (no browser available):
   Do the interactive steps above ONCE, locally. Then open the resulting
   yahoo_token.json and copy the "refresh_token" value -- that's the only thing
   CI needs. Store these three as GitHub repo secrets (Settings > Secrets and
   variables > Actions):
     YAHOO_CLIENT_ID
     YAHOO_CLIENT_SECRET
     YAHOO_REFRESH_TOKEN
   When YAHOO_REFRESH_TOKEN is set as an env var and no local yahoo_token.json
   exists, this script skips the interactive flow entirely and exchanges the
   refresh token for a fresh access token automatically -- see _load_token().
   Never commit yahoo_token.json to the repo (it contains live credentials);
   add it to .gitignore.
"""
import os
import json
import time
import base64
from urllib.parse import urlencode, urlparse, parse_qs

import requests

CLIENT_ID = os.environ.get("YAHOO_CLIENT_ID", "PASTE_YOUR_CLIENT_ID_HERE")
CLIENT_SECRET = os.environ.get("YAHOO_CLIENT_SECRET", "PASTE_YOUR_CLIENT_SECRET_HERE")
REDIRECT_URI = "https://localhost:8000"
SCOPE = "fspt-r"  # Fantasy Sports read-only

AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
TOKEN_FILE = "yahoo_token.json"


def _basic_auth_header():
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('utf-8')}"}


def _save_token(data):
    data["obtained_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    # No local token file (e.g. running in CI) -- bootstrap from a refresh token
    # supplied via env var / GitHub Secret. expires_in/obtained_at are set to force
    # an immediate refresh below, since we only have the refresh token, not a
    # cached access token.
    env_refresh = os.environ.get("YAHOO_REFRESH_TOKEN")
    if env_refresh:
        return {"refresh_token": env_refresh, "expires_in": 0, "obtained_at": 0}
    return None


def _extract_code(user_input):
    """Accepts either a raw code, or the full redirected URL containing ?code=..."""
    user_input = user_input.strip()
    if user_input.startswith("http"):
        qs = parse_qs(urlparse(user_input).query)
        return qs["code"][0]
    return user_input


def first_time_auth():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "language": "en-us",
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    print("\nOpen this URL, log into Yahoo, and click Agree:\n")
    print(url)
    print("\nAfter approving, your browser will land on a page that fails to load")
    print("(nothing runs on localhost:8000 -- that's expected). Copy the FULL URL")
    print("from the address bar (it contains '?code=...') and paste it below.\n")
    raw = input("Paste the redirect URL or just the code: ")
    code = _extract_code(raw)

    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    resp.raise_for_status()
    token_data = resp.json()
    _save_token(token_data)
    print("\nAuth successful. Token cached in yahoo_token.json.")
    return token_data


def refresh_token(token_data):
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "redirect_uri": REDIRECT_URI,
            "refresh_token": token_data["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_token = resp.json()
    # Yahoo's refresh response sometimes omits refresh_token if unchanged -- keep the old one
    new_token.setdefault("refresh_token", token_data["refresh_token"])
    _save_token(new_token)
    return new_token


def get_valid_access_token():
    """Main entry point other scripts should call. Returns a bearer token string,
    refreshing (or doing first-time auth) as needed."""
    token_data = _load_token()
    if token_data is None:
        token_data = first_time_auth()

    expires_in = token_data.get("expires_in", 3600)
    obtained_at = token_data.get("obtained_at", 0)
    if time.time() > obtained_at + expires_in - 60:  # 60s safety margin
        print("Access token expired -- refreshing...")
        token_data = refresh_token(token_data)

    return token_data["access_token"]


def api_get(path):
    """Convenience wrapper: GET a Fantasy Sports API path, JSON format, auto-auth."""
    token = get_valid_access_token()
    url = f"https://fantasysports.yahooapis.com/fantasy/v2/{path}"
    sep = "&" if "?" in url else "?"
    r = requests.get(f"{url}{sep}format=json", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    if "PASTE_YOUR" in CLIENT_ID:
        raise SystemExit("Set YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET (env vars or edit this file) first.")
    data = api_get("users;use_login=1/games")
    print(json.dumps(data, indent=2)[:800])
    print("\nAuth working -- run yahoo_kepners_history.py next.")
