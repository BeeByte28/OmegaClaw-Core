import json
import os
import time
import urllib.request

_proxy_url = None
_auth_enabled = None
_CHANNEL_DIR_NAME = ".channel"
_CHANNEL_AUTH_USER_FILE = "authenticated-user.json"
_user_ID_processed = False


def get_proxy_url():
    global _proxy_url
    if _proxy_url is None:
        _proxy_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
    return _proxy_url


def is_auth_enabled():
    global _auth_enabled
    if _auth_enabled is not None:
        return _auth_enabled
    proxy = get_proxy_url()
    if not proxy:
        _auth_enabled = False
        return False
    try:
        url = f"{proxy}/auth/status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            _auth_enabled = data.get("enabled", False)
    except Exception:
        _auth_enabled = False
    return _auth_enabled


def verify_token(candidate):
    proxy = get_proxy_url()
    if not proxy:
        return True
    url = f"{proxy}/auth/verify"
    req = urllib.request.Request(url)
    req.add_header("X-Auth-Token", str(candidate))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("match", False)
    except Exception:
        return False


def _channel_auth_user_path():
    memory_dir = os.environ["MEMORY_DIR"]
    return os.path.join(memory_dir, _CHANNEL_DIR_NAME, _CHANNEL_AUTH_USER_FILE)


def store_channel_authenticated_user_id(channel_identifier, user_id):
    # For any single run of OmegaClaw, allow only a single save of a user-id or verification    
    global _user_ID_processed
    if _user_ID_processed:
        return False
    
    """Record an authenticated channel user ID in the memory directory."""
    channel_identifier = str(channel_identifier or "").strip() or "DEFAULT"
    user_id = str(user_id).strip()
    if not user_id:
        raise ValueError("user_id is required")
    payload = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channel_identifier": channel_identifier,
        "user_id": user_id,
    }
    path = _channel_auth_user_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
            f.write("\n")
    except OSError as e:
        raise RuntimeError("Failed to write channel authenticated user record") from e
    _user_ID_processed = True
    return True


def get_channel_saved_user_id(channel_identifier, user_id):
    # For any single run of OmegaClaw, allow only a single save of a user-id or verification    
    global _user_ID_processed
    if _user_ID_processed:
        return False

    channel_identifier = str(channel_identifier or "").strip() or "DEFAULT"
    user_id = str(user_id).strip()
    if not user_id:
        return False
    try:
        path = _channel_auth_user_path()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                    saved_channel_identifier = str(record.get("channel_identifier", "")).strip()
                    saved_user_id = str(record.get("user_id", "")).strip()
                except (AttributeError, json.JSONDecodeError):
                    continue
                if saved_channel_identifier == channel_identifier and saved_user_id == user_id:
                    _user_ID_processed = True
                    return True
    except Exception as e:
        raise RuntimeError("Failed to read channel authenticated user records") from e
    return False

def authenticate_channel_user(channel_identifier, user_id, candidate):
    # Check if there is a valid "auth <string>" token. 
    # else see if there was a prior session with the user-id and channel.
    # Otherwise ignore.
    if verify_token(candidate) and \
       store_channel_authenticated_user_id(channel_identifier, user_id):
            label = str(channel_identifier).upper()
            print(f"[{label}] Saved authenticated user ID")
            return "auth_bound"
    elif get_channel_saved_user_id(channel_identifier, user_id):
        label = str(channel_identifier).upper()
        print(f"[{label}] Verified previously validated user ID")
        return "allow"
    else:
        return "ignore"
