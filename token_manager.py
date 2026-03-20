"""
Token Manager – generates and saves a fresh Upstox access token.
Run this ONCE each morning before starting the bot:

    python token_manager.py

Steps:
  1. Opens Upstox login page in browser
  2. You log in and are redirected to 127.0.0.1 (the redirect URI)
  3. Paste that redirect URL here
  4. Script exchanges the code for an access_token and saves it to .env
"""

import os
import webbrowser
import requests
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv, set_key

load_dotenv()

API_KEY      = os.getenv("UPSTOX_API_KEY", "")
API_SECRET   = os.getenv("UPSTOX_API_SECRET", "")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1/")
ENV_FILE     = ".env"

AUTH_URL  = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


def generate_token() -> None:
    if not API_KEY or not API_SECRET:
        print("ERROR: Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env first")
        return

    # Step 1 – Build login URL
    login_url = (
        f"{AUTH_URL}"
        f"?response_type=code"
        f"&client_id={API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
    )

    print("\nOpening Upstox login page in browser...")
    print(f"URL: {login_url}\n")
    webbrowser.open(login_url)

    # Step 2 – Get redirect URL from user
    redirect_url = input(
        "After login, paste the full redirect URL here\n"
        "(looks like: http://127.0.0.1/?code=XXXXXX)\n> "
    ).strip()

    params = parse_qs(urlparse(redirect_url).query)
    code   = params.get("code", [None])[0]

    if not code:
        print("ERROR: Could not extract authorization code from URL")
        return

    # Step 3 – Exchange code for access token
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code":          code,
            "client_id":     API_KEY,
            "client_secret": API_SECRET,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        },
        timeout=10,
    )

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed\n{resp.text}")
        return

    access_token = resp.json().get("access_token")
    if not access_token:
        print(f"ERROR: No access_token in response\n{resp.json()}")
        return

    # Step 4 – Save to .env
    if not os.path.exists(ENV_FILE):
        if os.path.exists(".env.example"):
            import shutil
            shutil.copy(".env.example", ENV_FILE)
        else:
            open(ENV_FILE, "w").close()

    set_key(ENV_FILE, "UPSTOX_ACCESS_TOKEN", access_token)
    print(f"\nAccess token saved to {ENV_FILE}")
    print("You can now start the bot:  python main.py")


if __name__ == "__main__":
    generate_token()
