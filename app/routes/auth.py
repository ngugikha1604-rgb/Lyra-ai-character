"""
routes/auth.py — YouTube OAuth flow.

  GET /authorize       — redirect user sang Google OAuth consent screen
  GET /oauth2callback  — nhận code, đổi lấy token, lưu credentials
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify, redirect, request, session, url_for, current_app
import google_auth_oauthlib.flow

CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
YOUTUBE_CREDENTIALS_FILE = os.environ.get(
    "YOUTUBE_CREDENTIALS_FILE", "youtube_credentials.json"
)

bp = Blueprint("auth", __name__)


def _save_credentials(credentials: dict) -> None:
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(credentials, f)
    except Exception as e:
        print(f"[Auth] Could not save credentials: {e}")


def _load_credentials() -> dict | None:
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        if isinstance(creds, dict) and creds.get("token"):
            return creds
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"[Auth] Credentials corrupted, deleting: {e}")
        try:
            os.remove(YOUTUBE_CREDENTIALS_FILE)
        except Exception:
            pass
    except Exception as e:
        print(f"[Auth] Could not load credentials: {e}")
    return None


@bp.route("/authorize")
def authorize():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"error": "client_secret.json not found"}), 500

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    flow.redirect_uri = url_for("auth.oauth2callback", _external=True)
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true"
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@bp.route("/oauth2callback")
def oauth2callback():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"error": "client_secret.json not found"}), 500

    state = session.get("oauth_state", "")
    flow  = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state
    )
    flow.redirect_uri = url_for("auth.oauth2callback", _external=True)
    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials
    creds_dict = {
        "token":         credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri":     credentials.token_uri,
        "client_id":     credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes":        credentials.scopes,
    }
    _save_credentials(creds_dict)

    # Inject vào app context để stream/start có thể dùng ngay
    current_app.yt_credentials = creds_dict

    return "OAuth thành công! Bạn có thể đóng tab này và bắt đầu stream."


# Helper để load credentials khi app khởi động
def try_load_saved_credentials() -> dict | None:
    return _load_credentials()
