"""
One-time interactive Google OAuth 2.0 Desktop App setup & status check for The Family Table.

Usage:
  python -I -X utf8 auth_setup.py           # authorize (opens browser once) and verify API access
  python -I -X utf8 auth_setup.py --check   # check current token without opening browser
  python -I -X utf8 auth_setup.py --reauth  # force fresh OAuth consent & refresh token
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import google_connector as gc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Google OAuth 2.0 setup for The Family Table")
    ap.add_argument("--config", default=None, help="Path to private config.json")
    ap.add_argument("--check", action="store_true", help="Non-interactive status check only")
    ap.add_argument("--reauth", action="store_true", help="Force re-authorization in browser")
    args = ap.parse_args()

    cfg, secrets_dir = gc.load_config(args.config)
    if args.check:
        st = gc.auth_status(cfg, secrets_dir)
        print(json.dumps(st, indent=2))
        return 0 if st.get("valid") else 1

    print("Starting Google OAuth 2.0 Desktop App authorization...")
    print("Scopes requested: Gmail (read + drafts), Google Calendar (read-only), Google Sheets (memory sheet)")
    creds = gc.get_credentials(cfg, secrets_dir, interactive=True, force_reauth=args.reauth)
    st = gc.auth_status(cfg, secrets_dir)
    if not st.get("valid"):
        print("Authorization failed:", st.get("error"), file=sys.stderr)
        return 1
    print(f"OK! Authorized Google account: {st.get('email')}")
    print("Token saved to private secrets folder (never synced to Git).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
