import json
import os
import urllib.request

_proxy_url = None
_auth_enabled = None


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


def store_authenticated_user_id(user_id):
    """Record an authenticated Slack user ID in the local nginx auth log."""
    proxy = get_proxy_url()
    if not proxy:
        return False
    url = f"{proxy}/auth/user"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("X-Authenticated-User-Id", str(user_id))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 204
    except Exception:
        return False


def get_saved_user_id():
    proxy = get_proxy_url()
    if not proxy:
        return None
    try:
        with urllib.request.urlopen(f"{proxy}/auth/users", timeout=5) as resp:
            lines = resp.read().decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return None

    for line in reversed(lines):
        try:
            user_id = str(json.loads(line).get("user_id", "")).strip()
        except (AttributeError, json.JSONDecodeError):
            continue
        if user_id:
            return user_id
    return None
