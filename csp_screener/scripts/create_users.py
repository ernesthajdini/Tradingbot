"""
Create Supabase Auth users for the dashboard (admin API).

Usage (PowerShell):
  $env:SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
  $env:SUPABASE_SERVICE_KEY = "eyJ...service_role key..."
  python csp_screener/scripts/create_users.py "user1@x.com:Password1" "user2@y.com:Password2"

Each argument is "email:password". Users are created pre-confirmed
(no verification email round-trip). Existing users are skipped.

SECURITY: passwords are passed as CLI args on YOUR machine only — they are
never written to the repo. Tell each user to change their password after
first login (Supabase → the dashboard has no self-serve change yet; the
admin can update via Authentication → Users → ... → Reset password).
"""

import os
import sys


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars first.")
        return 1

    specs = sys.argv[1:]
    if not specs:
        print("ERROR: pass at least one 'email:password' argument.")
        return 1

    try:
        from supabase import create_client
    except ImportError:
        print("ERROR: pip install supabase")
        return 1

    client = create_client(url, key)

    ok = 0
    for spec in specs:
        if ":" not in spec:
            print(f"SKIP (bad format, want email:password): {spec}")
            continue
        email, password = spec.split(":", 1)
        email = email.strip()
        try:
            client.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,   # no verification email needed
            })
            print(f"CREATED: {email}")
            ok += 1
        except Exception as e:
            msg = str(e)
            if "already been registered" in msg or "already exists" in msg.lower():
                print(f"EXISTS (skipped): {email}")
                ok += 1
            else:
                print(f"FAILED: {email} -> {msg}")

    print(f"\n{ok}/{len(specs)} users ready.")
    return 0 if ok == len(specs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
