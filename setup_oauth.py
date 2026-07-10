"""
Run this script ONCE locally to obtain a refresh token.
The token is then stored as a GitHub Secret — never committed to the repo.

Usage:
    pip install google-auth-oauthlib
    python setup_oauth.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/adwords",
    # Read-only Tag Manager access, used by the analyze_history GTM step to
    # fill the Nome_Tag_GTM column. Added 2026-07: tokens consented before
    # this date lack it — re-run this script and update GOOGLE_REFRESH_TOKEN.
    "https://www.googleapis.com/auth/tagmanager.readonly",
]

# Path to the client_secret JSON downloaded from Google Cloud Console
CLIENT_SECRETS_FILE = "client_secret.json"


def main():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    credentials = flow.run_local_server(port=0)

    print("\n--- Copy these values to GitHub Secrets ---")
    print(f"GOOGLE_CLIENT_ID:     {credentials.client_id}")
    print(f"GOOGLE_CLIENT_SECRET: {credentials.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN: {credentials.refresh_token}")
    print("-------------------------------------------\n")
    print("IMPORTANT: Do NOT commit client_secret.json or these values to git.")


if __name__ == "__main__":
    main()
