"""One-time YouTube consent: get a refresh token for a channel and store it.

    .venv/bin/python scripts/youtube_auth.py --profile terraria \
        --client-file /mnt/c/Users/<you>/Downloads/client_secret_XXXX.json --gh

1. Reads the OAuth client id/secret from the JSON file Google Cloud Console
   offers for a "Desktop app" client (or from YOUTUBE_CLIENT_ID/SECRET already
   in .env when --client-file is omitted).
2. Prints a Google consent URL. Open it, choose the account that owns the
   CHANNEL, accept the "Google hasn't verified this app" warning (Advanced >
   Go to ...), and allow.
3. Google redirects the browser to http://localhost:8765/ - this script is
   listening there (WSL forwards localhost from Windows), exchanges the code
   for a refresh token, and writes it to .env as
   YOUTUBE_REFRESH_TOKEN_<PROFILE> (plain YOUTUBE_REFRESH_TOKEN for main).
4. With --gh, also stores the client id/secret and the refresh token as GitHub
   Actions secrets via `gh secret set`, piped on stdin.

No secret is ever printed. Needs no input on stdin, so it can run in the
background while you click through the browser.
"""

import argparse
import json
import secrets
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.publish.youtube import SCOPES, TOKEN_URL  # noqa: E402

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _env_name(base: str, profile: str) -> str:
    return base if profile == "main" else f"{base}_{profile.upper()}"


def _read_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict) -> None:
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def _gh_secret(name: str, value: str) -> None:
    subprocess.run(["gh", "secret", "set", name], input=value, text=True,
                   check=True, cwd=ROOT, capture_output=True)
    print(f"  GitHub secret {name} set")


def _wait_for_code(state: str) -> str:
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = ("YouTube access granted. You can close this tab."
                   if ok else f"Authorisation failed: {result.get('error', 'state mismatch')}")
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    while not result:
        server.handle_request()
    server.server_close()
    if "error" in result:
        raise SystemExit(f"Google returned an error: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("State mismatch - not the redirect this script started. Run it again.")
    return result["code"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="main")
    ap.add_argument("--client-file", help="client_secret_*.json downloaded from Google Cloud")
    ap.add_argument("--gh", action="store_true", help="also set the GitHub Actions secrets")
    args = ap.parse_args()

    env = _read_env()
    if args.client_file:
        data = json.loads(Path(args.client_file).read_text())
        client = data.get("installed") or data.get("web") or {}
        client_id, client_secret = client.get("client_id"), client.get("client_secret")
        if not (client_id and client_secret):
            raise SystemExit("That file has no client_id/client_secret - download the "
                             "JSON for a Desktop app OAuth client.")
        if "installed" not in data:
            print("Warning: this is not a Desktop app client; the localhost redirect may be refused.")
    else:
        client_id, client_secret = env.get("YOUTUBE_CLIENT_ID"), env.get("YOUTUBE_CLIENT_SECRET")
        if not (client_id and client_secret):
            raise SystemExit("No --client-file and no YOUTUBE_CLIENT_ID/SECRET in .env.")

    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",        # forces a refresh token even on a repeat consent
        "state": state,
    })
    print("Open this URL in your browser and sign in as the account that owns the channel:\n")
    print(url, "\n", flush=True)
    print(f"Waiting for Google to redirect to {REDIRECT} ...", flush=True)

    code = _wait_for_code(state)
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }, timeout=20)
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")
    refresh = resp.json().get("refresh_token")
    if not refresh:
        raise SystemExit("Google returned no refresh token. Remove the app's access at "
                         "https://myaccount.google.com/permissions and run this again.")

    # Prove it works and show which channel it belongs to.
    access = resp.json()["access_token"]
    ch = requests.get("https://www.googleapis.com/youtube/v3/channels",
                      params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {access}"}, timeout=15).json()
    items = ch.get("items") or []
    if not items:
        raise SystemExit("Signed-in account has no YouTube channel - create the channel first, "
                         "or pick the channel's account/brand account at the consent screen.")
    print(f"Authorised for channel: {items[0]['snippet']['title']}")

    token_name = _env_name("YOUTUBE_REFRESH_TOKEN", args.profile)
    _write_env({"YOUTUBE_CLIENT_ID": client_id, "YOUTUBE_CLIENT_SECRET": client_secret,
                token_name: refresh})
    print(f"Wrote YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and {token_name} to .env")

    if args.gh:
        _gh_secret("YOUTUBE_CLIENT_ID", client_id)
        _gh_secret("YOUTUBE_CLIENT_SECRET", client_secret)
        _gh_secret(token_name, refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
