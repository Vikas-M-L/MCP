"""
One-time Google OAuth setup.
Run this ONCE to authorize Gmail + Calendar access and save token.json.
After this, main.py will work without any browser popup.

Usage:
  python setup_google_auth.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import get_settings
from mcp_server.google_auth import SCOPES

cfg = get_settings()

print("=" * 55)
print("  Google OAuth Setup — PersonalOS Agent")
print("=" * 55)

# Check credentials.json
if not Path(cfg.google_credentials_path).exists():
    print(f"\n[ERROR] credentials.json not found at: {cfg.google_credentials_path}")
    print("\nTo fix:")
    print("  1. Go to https://console.cloud.google.com")
    print("  2. APIs & Services → Credentials → Create OAuth 2.0 Client ID")
    print("  3. Application type: Desktop app")
    print("  4. Download JSON → rename to credentials.json → place in project folder")
    sys.exit(1)

print(f"\n[OK] credentials.json found: {cfg.google_credentials_path}")
print(f"[  ] token.json will be saved to: {cfg.google_token_path}")
print("\nA browser window will open. Sign in and grant access to:")
for scope in SCOPES:
    name = scope.split("/")[-1]
    print(f"  • {name}")

print("\nWaiting for browser authorization...")

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(cfg.google_credentials_path, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(cfg.google_token_path, "w") as f:
        f.write(creds.to_json())

    print(f"\n[OK] token.json saved to: {cfg.google_token_path}")

    # Quick verification
    from googleapiclient.discovery import build
    gmail = build("gmail", "v1", credentials=creds)
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"[OK] Gmail verified — account: {profile.get('emailAddress')}")

    cal = build("calendar", "v3", credentials=creds)
    cal_list = cal.calendarList().list(maxResults=1).execute()
    items = cal_list.get("items", [])
    cal_name = items[0].get("summary", "primary") if items else "primary"
    print(f"[OK] Calendar verified — calendar: '{cal_name}'")

    print("\n" + "=" * 55)
    print("  Setup complete! Now run: python main.py")
    print("=" * 55)

except Exception as e:
    print(f"\n[ERROR] OAuth failed: {e}")
    sys.exit(1)
