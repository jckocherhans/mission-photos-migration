#!/usr/bin/env python3
"""Two-step Google sign-in for the migration script.

  python3 auth.py start            -> prints a URL for Jason to open and approve
  python3 auth.py finish "<url>"   -> paste the http://localhost:8765/?... address he lands on
                                      writes token.json

Run both steps from the same directory: flow.pkl holds the one-time PKCE verifier.
"""
import os
import pickle
import sys

from google_auth_oauthlib.flow import Flow

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
REDIRECT = "http://localhost:8765/"
SECRETS = os.path.join(HERE, "client_secret.json")
STATE = os.path.join(HERE, "flow.pkl")
TOKEN = os.path.join(HERE, "token.json")


def start():
    flow = Flow.from_client_secrets_file(
        SECRETS, scopes=SCOPES, redirect_uri=REDIRECT, autogenerate_code_verifier=True
    )
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    with open(STATE, "wb") as f:
        pickle.dump({"verifier": flow.code_verifier, "state": state}, f)
    os.chmod(STATE, 0o600)
    print(url)


def finish(redirect_url):
    with open(STATE, "rb") as f:
        st = pickle.load(f)
    flow = Flow.from_client_secrets_file(
        SECRETS, scopes=SCOPES, redirect_uri=REDIRECT,
        code_verifier=st["verifier"], state=st["state"],
    )
    # oauthlib insists on https for the redirect it parses; the scheme is not otherwise used.
    flow.fetch_token(authorization_response=redirect_url.replace("http://", "https://", 1))
    with open(TOKEN, "w") as f:
        f.write(flow.credentials.to_json())
    os.chmod(TOKEN, 0o600)
    os.remove(STATE)
    print("token.json written")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "start":
        start()
    elif len(sys.argv) >= 3 and sys.argv[1] == "finish":
        finish(sys.argv[2])
    else:
        print(__doc__)
