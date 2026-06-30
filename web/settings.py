from __future__ import annotations

import json
import os

from paths import ROOT

SETTINGS_FILE = ROOT / "settings.json"

DEFAULTS = {
    "proxy_concurrency": 40,
    "settle_enabled": True, "settle_music": "",
    "notify_on_win": True, "notify_on_done": False, "notify_on_risk": False,
    "tg_enabled": False, "tg_token": "", "tg_chat": "", "tg_proxy": "",
    "webhook_enabled": False, "webhook_url": "",
    "smtp_enabled": False, "smtp_host": "", "smtp_port": 465,
    "smtp_user": "", "smtp_pass": "", "smtp_to": "",
}


def load() -> dict:
    d = dict(DEFAULTS)
    try:
        if SETTINGS_FILE.exists():
            d.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return d


def save(s: dict) -> dict:
    cur = load()
    for k, v in (s or {}).items():
        if k in DEFAULTS:
            cur[k] = v
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(cur, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_FILE)
    return cur
