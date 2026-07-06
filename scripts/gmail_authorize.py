"""One-time local helper: authorize a family member's Gmail for DRAFT creation.

Run this on a machine WITH A BROWSER (not the headless container). It opens
Google's consent screen and writes a per-member token file the app later loads
read-only. One token per opted-in member; everyone else never grants access.

Prereqs:
  * A Google Cloud OAuth *Desktop app* client — download its client_secret.json.
  * `pip install google-auth-oauthlib`.

Usage:
  python scripts/gmail_authorize.py \
      --number 447700900000 \
      --client-secrets ~/Downloads/client_secret.json \
      --out ./secrets/gmail

Then mount that --out directory read-only into the app container at
GMAIL_TOKENS_DIR (see docker-compose.yml). The scope requested is gmail.compose
(draft management). The assistant only ever creates drafts — it never sends
(spec §9/§10).
"""
import argparse
import os

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", required=True, help="Member's WhatsApp number (digits, no '+').")
    parser.add_argument("--client-secrets", required=True, help="Path to the OAuth client_secret.json.")
    parser.add_argument("--out", default="./secrets/gmail", help="Directory to write <number>.json into.")
    args = parser.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.number}.json")
    with open(path, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(path, 0o600)
    print(f"Wrote {path}. Mount this directory read-only at GMAIL_TOKENS_DIR to enable email for {args.number}.")


if __name__ == "__main__":
    main()
