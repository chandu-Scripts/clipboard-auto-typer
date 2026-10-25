"""Local Speechmatics credentials and short-lived browser authentication.

The long-lived key never leaves this module except in an HTTPS authorization
header to Speechmatics. Windows DPAPI binds the saved key to this Windows user.
"""

import base64
import json
import os
from pathlib import Path
import threading

import requests


CONFIG_PATH = Path(__file__).with_name("speech_credentials.json")
TOKEN_URL = "https://mp.speechmatics.com/v1/api_keys?type=rt"
REALTIME_URL = "wss://eu.rt.speechmatics.com/v2"
_lock = threading.RLock()


class SpeechConfigError(Exception):
    """A safe, user-facing error that contains no credentials."""


def _protect(value):
    import win32crypt

    return win32crypt.CryptProtectData(value, "Answer Board Speechmatics", None, None, None, 1)


def _unprotect(value):
    import win32crypt

    return win32crypt.CryptUnprotectData(value, None, None, None, 1)[1]


def _validate(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 4096:
        raise SpeechConfigError("Paste a valid Speechmatics API key first.")
    key = key.strip()
    if any(char.isspace() or ord(char) < 32 or ord(char) > 126 for char in key):
        raise SpeechConfigError("The API key must not contain spaces or control characters.")
    return key


def save_key(key):
    key = _validate(key)
    with _lock:
        try:
            encrypted = base64.b64encode(_protect(key.encode("utf-8"))).decode("ascii")
            temporary = CONFIG_PATH.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({"version": 1, "encrypted_key": encrypted}), encoding="utf-8")
            os.replace(temporary, CONFIG_PATH)
        except Exception:
            raise SpeechConfigError("Could not save the key securely for this Windows user. Check pywin32 and folder permissions.") from None


def remove_key():
    with _lock:
        try:
            CONFIG_PATH.unlink(missing_ok=True)
        except OSError:
            raise SpeechConfigError("Could not remove the saved key. Check folder permissions.") from None


def load_key():
    with _lock:
        if not CONFIG_PATH.exists():
            raise SpeechConfigError("Add your Speechmatics API key in Speech settings first.")
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return _validate(_unprotect(base64.b64decode(data["encrypted_key"], validate=True)).decode("utf-8"))
        except Exception:
            raise SpeechConfigError("The saved key cannot be opened by this Windows user. Save your key again.") from None


def key_status():
    with _lock:
        if not CONFIG_PATH.exists():
            return {"configured": False, "message": "No Speechmatics key saved on this laptop."}
        try:
            load_key()
            return {"configured": True, "message": "Key saved for this Windows user."}
        except SpeechConfigError as exc:
            return {"configured": False, "message": str(exc)}


def create_token(key=None):
    """Mint a 60-second session-start token; never return upstream error bodies."""
    key = load_key() if key is None else _validate(key)
    try:
        response = requests.post(
            TOKEN_URL, headers={"Authorization": "Bearer " + key},
            json={"ttl": 60}, timeout=(5, 12), allow_redirects=False,
        )
    except requests.RequestException:
        raise SpeechConfigError("Could not reach Speechmatics. Check your internet connection and try again.") from None
    with response:
        if response.status_code in (401, 403):
            raise SpeechConfigError("Speechmatics rejected this key. Check the key and account permissions.")
        if response.status_code == 429:
            raise SpeechConfigError("Speechmatics is rate limiting requests. Wait a moment and retry.")
        if not 200 <= response.status_code < 300:
            raise SpeechConfigError("Speechmatics could not authorize a session. Check your account and try again.")
        try:
            token = response.json()["key_value"]
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise SpeechConfigError("Speechmatics returned an invalid session token. Try again later.") from None
    return {"token": token, "url": REALTIME_URL}
