"""Configuration: defaults, loading and saving /config/config.json."""
import copy
import json
import os
import secrets
import threading

CONFIG_DIR = os.environ.get("NEXTARR_CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    # Safety first: a fresh install only logs what it would do.
    "dry_run": True,
    "sonarr": {"url": "http://sonarr:8989", "api_key": ""},
    "tautulli": {"url": "http://tautulli:8181", "api_key": ""},
    "plex": {"url": "https://plex:32400", "token": "", "verify_ssl": False},
    "overseerr": {"url": "", "api_key": ""},
    # Rules
    "episodes_ahead": 2,
    "active_days": 120,
    "delete_grace_days": 14,
    "exclude_tag": "keep",
    "include_specials": False,
    # Scheduling
    "sync_interval_minutes": 15,
    "cleanup_hour": 4,
    # Deletion safety
    "approval_threshold": 20,
    "approval_expiry_days": 7,
    "availability_check": True,
    "availability_recheck_days": 30,
    "max_availability_checks_per_run": 40,
    # Notifications and links
    "discord_webhook": "",
    "public_url": "",
    "webhook_token": "",
    # Web UI login (optional)
    "auth": {"enabled": False, "username": "admin", "password_hash": ""},
}

SECRET_FIELDS = {("sonarr", "api_key"), ("tautulli", "api_key"), ("plex", "token"),
                 ("overseerr", "api_key"), ("discord_webhook",), ("webhook_token",),
                 ("auth", "password_hash")}

_lock = threading.Lock()


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        cfg = _merge(DEFAULTS, data)
        if not cfg["webhook_token"]:
            cfg["webhook_token"] = secrets.token_urlsafe(24)
            _write(cfg)
        return cfg


def _write(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def save(cfg):
    with _lock:
        _write(cfg)


def paused():
    return os.path.exists(os.path.join(CONFIG_DIR, "PAUSE"))
