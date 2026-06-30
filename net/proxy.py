from __future__ import annotations

import os
from urllib.parse import urlsplit, quote

_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def _host_like(s: str) -> bool:
    return ("." in s) or (s == "localhost")


def _build(scheme: str, host: str, port: str, user: str = "", pwd: str = "") -> str | None:
    if not (host and port and port.isdigit()):
        return None
    scheme = scheme.lower()
    if scheme not in _SCHEMES:
        scheme = "http"
    if scheme == "socks5":
        scheme = "socks5h"
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
    h = f"[{host}]" if ":" in host else host
    return f"{scheme}://{auth}{h}:{port}"


def parse_proxy(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()

    if "://" in s:
        sp = urlsplit(s)
        scheme = sp.scheme.lower()
        host = sp.hostname or ""
        port = str(sp.port) if sp.port else ""
        if not port and ":" in sp.netloc.split("@")[-1]:
            port = sp.netloc.split("@")[-1].split(":")[-1]
        return _build(scheme, host, port, sp.username or "", sp.password or "")

    if "@" in s:
        cred, _, hostport = s.rpartition("@")
        user, _, pwd = cred.partition(":")
        host, _, port = hostport.rpartition(":")
        return _build("http", host, port, user, pwd)

    parts = s.split(":")
    if len(parts) == 2:
        return _build("http", parts[0], parts[1])
    if len(parts) == 4:
        a, b, c, d = parts
        a_is_host = _host_like(a) and b.isdigit()
        c_is_host = _host_like(c) and d.isdigit()
        if a_is_host and not c_is_host:
            return _build("http", a, b, c, d)
        if c_is_host and not a_is_host:
            return _build("http", c, d, a, b)
        if d.isdigit() and not b.isdigit():
            return _build("http", c, d, a, b)
        if b.isdigit():
            return _build("http", a, b, c, d)
        return None
    if len(parts) == 3 and parts[1].isdigit():
        return _build("http", parts[0], parts[1], parts[2], "")
    return None


def resolve_pool(raws: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in (raws or []):
        raw = str(raw).strip()
        if not raw:
            continue
        path = raw[1:].strip() if raw.startswith("@") else raw
        is_file = (raw.startswith("@") or "://" not in raw) and path and os.path.isfile(path)
        if is_file:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            out.append(line)
            except Exception:
                pass
        else:
            out.append(raw)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def proxy_label(normalized: str | None) -> str:
    if not normalized:
        return "直连"
    sp = urlsplit(normalized)
    auth = "*:*@" if sp.username else ""
    h = f"[{sp.hostname}]" if sp.hostname and ":" in sp.hostname else sp.hostname
    return f"{sp.scheme}://{auth}{h}:{sp.port}"
