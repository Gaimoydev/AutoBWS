from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from net.http import DEFAULT_IMPERSONATE
from paths import PROFILES_DIR


def _safe_name(name: str) -> str:
    s = re.sub(r"[^\w一-鿿.\-]+", "_", (name or "").strip())
    return (s[:60] or "profile")


_STOP_POLICY_DEFAULT = {"success": "session", "soldout": "session", "limit": "daytype"}
_STOP_POLICY_ALLOWED = {
    "success": ("session", "daytype", "account"),
    "soldout": ("none", "session"),
    "limit": ("none", "session", "daytype", "account"),
}


def _coerce_stop_policy(raw) -> dict:
    out = dict(_STOP_POLICY_DEFAULT)
    if isinstance(raw, dict):
        for k, allowed in _STOP_POLICY_ALLOWED.items():
            if raw.get(k) in allowed:
                out[k] = raw[k]
    return out


_PACE_POLICY_DEFAULT = {
    "relief_ms": 120,
    "throttle": {"mode": "auto", "value": 300},
    "risk": {"mode": "auto", "value": 800},
    "curve": "accel",
    "max_ms": 1500,
    "jitter_ms": 40,
}


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_state(raw, default: dict) -> dict:
    out = dict(default)
    if isinstance(raw, dict):
        if raw.get("mode") in ("auto", "fixed"):
            out["mode"] = raw["mode"]
        out["value"] = max(1, _as_int(raw.get("value"), default["value"]))
    return out


def _coerce_pace_policy(raw, base: int = 300) -> dict:
    d = _PACE_POLICY_DEFAULT
    base = max(1, _as_int(base, 300))
    if not isinstance(raw, dict):
        raw = {}
    max_ms = max(base, _as_int(raw.get("max_ms"), d["max_ms"]))
    curve = raw.get("curve") if raw.get("curve") in ("linear", "accel") else d["curve"]
    return {
        "relief_ms": min(max_ms, max(1, _as_int(raw.get("relief_ms"), d["relief_ms"]))),
        "throttle": _coerce_state(raw.get("throttle"), d["throttle"]),
        "risk": _coerce_state(raw.get("risk"), d["risk"]),
        "curve": curve,
        "max_ms": max_ms,
        "jitter_ms": max(0, _as_int(raw.get("jitter_ms"), d["jitter_ms"])),
    }


@dataclass
class Profile:
    name: str
    uid: str = ""
    uname: str = ""
    face: str = ""
    impersonate: str = DEFAULT_IMPERSONATE
    proxies: list = field(default_factory=list)
    fallback_direct: bool = True
    cookies: list = field(default_factory=list)
    base_interval: int = 300
    offset: int = 50
    sessions: list = field(default_factory=list)
    stop_policy: dict = field(default_factory=lambda: dict(_STOP_POLICY_DEFAULT))
    pace_policy: dict = field(default_factory=lambda: copy.deepcopy(_PACE_POLICY_DEFAULT))
    updated_at: int = 0

    @property
    def filename(self) -> str:
        return f"{_safe_name(self.name)}.json"


def _coerce(d: dict) -> Profile:
    fields = Profile.__dataclass_fields__
    kw = {k: d[k] for k in fields if k in d}
    if "name" not in kw:
        kw["name"] = "未命名"
    if "proxies" not in kw and d.get("proxy"):
        kw["proxies"] = [d["proxy"]]
    if not isinstance(kw.get("proxies"), list):
        kw["proxies"] = [kw["proxies"]] if kw.get("proxies") else []
    for lf in ("sessions", "cookies"):
        if lf in kw and not isinstance(kw[lf], list):
            kw[lf] = []
    kw["stop_policy"] = _coerce_stop_policy(kw.get("stop_policy"))
    kw["pace_policy"] = _coerce_pace_policy(kw.get("pace_policy"), kw.get("base_interval", 300))
    return Profile(**kw)


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load(name: str) -> Profile | None:
    p = PROFILES_DIR / f"{_safe_name(name)}.json"
    if not p.exists():
        return None
    try:
        return _coerce(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def load_all(names: list[str] | None = None) -> list[Profile]:
    names = names if names is not None else list_profiles()
    out = []
    for n in names:
        pr = load(n)
        if pr:
            out.append(pr)
    return out


def save(profile: Profile) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile.updated_at = int(time.time())
    path = PROFILES_DIR / profile.filename
    path.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def delete(name: str) -> bool:
    p = PROFILES_DIR / f"{_safe_name(name)}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def session_snapshot(o: dict) -> dict:
    return {
        "reserve_id": o.get("reserve_id"),
        "ticket_no": o.get("ticket_no"),
        "title": o.get("title", ""),
        "location": o.get("location", ""),
        "date": o.get("date", ""),
        "type": o.get("type", 0),
        "type_name": o.get("type_name", ""),
        "begin": o.get("begin", 0),
        "end": o.get("end", 0),
        "act_begin": o.get("act_begin", 0),
        "act_end": o.get("act_end", 0),
        "stock": o.get("stock", 0),
        "total": o.get("total", 0),
    }
