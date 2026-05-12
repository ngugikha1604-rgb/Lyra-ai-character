"""
routes/auth.py — YouTube OAuth flow.

  GET /authorize       — redirect user sang Google OAuth consent screen
  GET /oauth2callback  — nhận code, đổi lấy token, lưu credentials

FIX PKCE: google-auth-oauthlib mới tự sinh code_verifier bên trong
authorization_url(). Phải đọc flow.code_verifier SAU khi gọi xong,
lưu vào memory dict (không dùng session vì cookie bị mất khi
localhost → 127.0.0.1 redirect).
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify, redirect, request, url_for, current_app
import google_auth_oauthlib.flow
import google.auth.transport.requests
import google.oauth2.credentials

CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
YOUTUBE_CREDENTIALS_FILE = os.environ.get(
    "YOUTUBE_CREDENTIALS_FILE", "youtube_credentials.json"
)

bp = Blueprint("auth", __name__)

# In-memory store — tránh hoàn toàn session cookie domain issue
# { state: code_verifier }
_oauth_store: dict[str, str] = {}


# ------------------------------------------------------------------ #
# Credential helpers                                                   #
# ------------------------------------------------------------------ #

def _save_credentials(credentials: dict) -> None:
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(credentials, f)
    except Exception as e:
        print(f"[Auth] Could not save credentials: {e}")


def _creds_dict_to_object(creds_dict: dict) -> google.oauth2.credentials.Credentials:
    return google.oauth2.credentials.Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        scopes=creds_dict.get("scopes"),
    )


def _refresh_credentials(creds_dict: dict) -> dict | None:
    try:
        creds = _creds_dict_to_object(creds_dict)
        if not creds.refresh_token:
            print("[Auth] Không có refresh_token — cần re-authorize.")
            return None
        creds.refresh(google.auth.transport.requests.Request())
        refreshed = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token or creds_dict.get("refresh_token"),
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes) if creds.scopes else creds_dict.get("scopes"),
        }
        _save_credentials(refreshed)
        print("[Auth] Token refreshed thành công.")
        return refreshed
    except Exception as e:
        print(f"[Auth] Refresh thất bại: {e}")
        return None


def _load_credentials() -> dict | None:
    try:
        with open(YOUTUBE_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        if not isinstance(creds, dict) or not creds.get("token"):
            return None
        credentials_obj = _creds_dict_to_object(creds)
        if credentials_obj.expired or not credentials_obj.valid:
            print("[Auth] Token hết hạn, đang refresh...")
            return _refresh_credentials(creds)
        return creds
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"[Auth] Credentials corrupted, deleting: {e}")
        try:
            os.remove(YOUTUBE_CREDENTIALS_FILE)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[Auth] Could not load credentials: {e}")
        return None


# ------------------------------------------------------------------ #
# Routes                                                               #
# ------------------------------------------------------------------ #

def _callback_uri() -> str:
    return url_for("auth.oauth2callback", _external=True).replace("localhost", "127.0.0.1")


@bp.route("/authorize")
def authorize():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"error": "client_secret.json not found"}), 500

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    flow.redirect_uri = _callback_uri()

    # Gọi authorization_url() TRƯỚC — thư viện sẽ tự sinh code_verifier bên trong
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    # ĐỌC code_verifier SAU khi authorization_url() đã chạy xong
    code_verifier = getattr(flow, "code_verifier", None)

    # Lưu vào memory dict, key = state (Google sẽ gửi lại state ở callback)
    _oauth_store[state] = code_verifier or ""
    print(f"[Auth] Authorize OK — state={state[:8]}... verifier={'SET' if code_verifier else 'NONE (PKCE disabled)'}")

    return redirect(auth_url)


@bp.route("/oauth2callback")
def oauth2callback():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"error": "client_secret.json not found"}), 500

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    state         = request.args.get("state", "")
    code_verifier = _oauth_store.pop(state, None)

    print(f"[Auth] Callback — state={state[:8]}... verifier={'OK' if code_verifier else 'NONE'}")

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state
    )
    flow.redirect_uri = _callback_uri()

    # Inject lại code_verifier vào flow mới trước khi fetch_token
    if code_verifier:
        flow.code_verifier = code_verifier

    auth_response = request.url.replace("localhost", "127.0.0.1")

    try:
        flow.fetch_token(authorization_response=auth_response)
    except Exception as e:
        print(f"[Auth] fetch_token error: {e}")
        return (
            f"<h3>OAuth thất bại</h3><pre>{e}</pre>"
            f"<p><a href='/authorize'>Thử lại</a></p>"
        ), 400

    credentials = flow.credentials
    creds_dict = {
        "token":         credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri":     credentials.token_uri,
        "client_id":     credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes":        list(credentials.scopes) if credentials.scopes else [],
    }
    _save_credentials(creds_dict)
    current_app.yt_credentials = creds_dict
    print("[Auth] OAuth hoàn tất, credentials đã lưu.")

    return "<h3>OAuth thành công! ✓</h3>Bạn có thể đóng tab này và bắt đầu stream."


def try_load_saved_credentials() -> dict | None:
    creds = _load_credentials()
    if creds:
        print("[Auth] Loaded YouTube credentials từ file.")
    else:
        print("[Auth] Không có credentials hợp lệ — cần /authorize.")
    return creds
