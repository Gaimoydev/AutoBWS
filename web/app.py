from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core import profiles, login, notify
from core.profiles import Profile, session_snapshot
from core.api import BwsClient, ID_TYPES, BIND_MESSAGES
from core.grabber import collect_sessions, selectable
from net.http import IMPERSONATE_CHOICES, DEFAULT_IMPERSONATE
from net.proxy import resolve_pool
from paths import STATIC_DIR, MUSIC_DIR
from web import settings as settings_store
from web.managers import LoginManager, ProxyChecker, GrabManager, LivenessMonitor

STATIC = STATIC_DIR
MUSIC = MUSIC_DIR

login_mgr = LoginManager()
proxy_chk = ProxyChecker()
grab_mgr = GrabManager()
liveness = LivenessMonitor()


@asynccontextmanager
async def _lifespan(app):
    liveness.start()
    yield
    grab_mgr.stop_all()


app = FastAPI(title="AUTOBWS", lifespan=_lifespan)

_LOCAL = {"127.0.0.1", "localhost", "::1"}
_ALLOW_LAN = os.environ.get("AUTOBWS_ALLOW_LAN") == "1"


def _host_of(value: str | None) -> str:
    if not value:
        return ""
    from urllib.parse import urlparse
    h = value.split("://", 1)[-1] if "://" in value else value
    return (urlparse("//" + h).hostname or h).split(":")[0].lower()


def _local_host(value: str | None) -> bool:
    return _host_of(value) in _LOCAL if value else True


def _loopback_ip(ip: str | None) -> bool:
    return bool(ip) and (ip in ("127.0.0.1", "::1") or ip.startswith("127."))


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _mutating(method: str) -> bool:
    return method in ("POST", "PUT", "DELETE", "PATCH")


@app.middleware("http")
async def _guard(request, call_next):
    if not _ALLOW_LAN:
        if request.client and not _loopback_ip(request.client.host):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not _local_host(request.headers.get("host")):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if _mutating(request.method) and not _local_host(request.headers.get("origin")):
            return JSONResponse({"error": "bad origin"}, status_code=403)
    elif _mutating(request.method):
        origin = request.headers.get("origin")
        if origin and _host_of(origin) != _host_of(request.headers.get("host")):
            return JSONResponse({"error": "bad origin"}, status_code=403)
    resp = await call_next(request)
    if request.method == "GET" and not request.url.path.startswith(("/api", "/ws")):
        resp.headers["Cache-Control"] = "no-cache"   # 静态资源每次校验,改了代码刷新即生效
    return resp


def brief(p: Profile) -> dict:
    return {"name": p.name, "uname": p.uname, "uid": p.uid, "face": p.face,
            "login_alive": liveness.alive_of(p.name), "impersonate": p.impersonate,
            "sessions": len(p.sessions), "proxies": _pcount(p.proxies),
            "fallback_direct": p.fallback_direct, "base_interval": p.base_interval,
            "offset": p.offset, "stop_policy": p.stop_policy, "pace_policy": p.pace_policy,
            "has_cookies": bool(p.cookies)}


def _pcount(proxies) -> int:
    try:
        return len(resolve_pool(proxies or []))
    except Exception:
        return len(proxies or [])


@app.get("/api/meta")
def meta():
    return {"impersonates": list(IMPERSONATE_CHOICES), "default_impersonate": DEFAULT_IMPERSONATE,
            "id_types": ID_TYPES}


@app.get("/api/profiles")
def list_profiles():
    return [brief(p) for n in profiles.list_profiles() if (p := profiles.load(n))]


@app.get("/api/profiles/{name}")
def get_profile(name: str):
    p = profiles.load(name) if name in profiles.list_profiles() else None
    if p is None:
        return JSONResponse({"error": "配置不存在或已损坏"}, status_code=404)
    d = brief(p)
    d["session_list"] = p.sessions
    return d


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    profiles.delete(name)
    return {"ok": True}


@app.post("/api/profiles")
async def save_profile(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "配置名不能为空"}, status_code=400)
    orig = body.get("orig_name")
    existing = profiles.load(orig) if (orig and orig in profiles.list_profiles()) else None

    cookies = login_mgr.cookies_of(body["login_id"]) if body.get("login_id") else None
    if not cookies and existing:
        cookies = existing.cookies
    if not cookies:
        return JSONResponse({"error": "尚未登录"}, status_code=400)
    info = await login.fetch_user_info(cookies, impersonate=body.get("impersonate", DEFAULT_IMPERSONATE))
    if not info:
        return JSONResponse({"error": "登录态校验失败"}, status_code=400)

    prof = existing or Profile(name=name)
    prof.uid, prof.uname = info["uid"], info["uname"]
    prof.face = info.get("face", "") or prof.face
    prof.impersonate = body.get("impersonate", DEFAULT_IMPERSONATE)
    prof.cookies = cookies
    if body.get("proxies") is not None:
        prof.proxies = list(body["proxies"])
    prof.fallback_direct = bool(body.get("fallback_direct", True))
    prof.base_interval = _int(body.get("base_interval"), prof.base_interval)
    if body.get("offset") is not None:
        prof.offset = profiles._coerce_offset(body["offset"])
    if body.get("stop_policy") is not None:
        prof.stop_policy = profiles._coerce_stop_policy(body["stop_policy"])
    if body.get("pace_policy") is not None:
        prof.pace_policy = profiles._coerce_pace_policy(body["pace_policy"], prof.base_interval)
    if body.get("sessions") is not None:
        prof.sessions = [session_snapshot(o) for o in body["sessions"] if selectable(o)]

    known = {orig, existing.name if existing else None}
    final = name
    if final not in known:
        ex = set(profiles.list_profiles()) - {x for x in known if x}
        if final in ex:
            i = 2
            while f"{final}_{i}" in ex:
                i += 1
            final = f"{final}_{i}"
    prof.name = final
    if orig and orig != final and orig in profiles.list_profiles():
        profiles.delete(orig)
    profiles.save(prof)
    return {"ok": True, "name": final, "renamed": final != name}


@app.post("/api/login/start")
async def login_start(body: dict):
    return await login_mgr.start(body.get("impersonate", DEFAULT_IMPERSONATE))


@app.get("/api/login/{sid}")
def login_status(sid: str):
    return login_mgr.status(sid)


async def _bound(cookies, impersonate):
    c = BwsClient(cookies, impersonate)
    try:
        return await c.is_bound()
    finally:
        await c.aclose()


async def _bind(cookies, impersonate, body):
    name = (body.get("name") or "").strip()
    pid = (body.get("personal_id") or "").strip()
    tk4 = (body.get("ticket4") or "").strip()
    idt = int(body.get("id_type", 0))
    if not (name and pid and len(tk4) == 4):
        return {"ok": False, "message": "姓名/证件号不能为空,票号必须后4位"}
    c = BwsClient(cookies, impersonate)
    try:
        r = await c.ticket_bind(name, pid, tk4, idt)
    finally:
        await c.aclose()
    code = r.get("code")
    return {"ok": code == 0, "code": code,
            "message": "绑定成功" if code == 0 else BIND_MESSAGES.get(code, r.get("message", "未知"))}


async def _sessions(cookies, impersonate):
    c = BwsClient(cookies, impersonate)
    try:
        return await collect_sessions(c)
    finally:
        await c.aclose()


def _login_ctx(sid):
    st = login_mgr.sessions.get(sid)
    return (st["cookies"], st["impersonate"]) if st and st.get("cookies") else (None, None)


@app.get("/api/login/{sid}/bound")
async def login_bound(sid: str):
    cookies, imp = _login_ctx(sid)
    if not cookies:
        return JSONResponse({"error": "未登录"}, status_code=400)
    return {"bound": await _bound(cookies, imp)}


@app.post("/api/login/{sid}/bind")
async def login_bind(sid: str, body: dict):
    cookies, imp = _login_ctx(sid)
    if not cookies:
        return JSONResponse({"error": "未登录"}, status_code=400)
    return await _bind(cookies, imp, body)


@app.get("/api/login/{sid}/sessions")
async def login_sessions(sid: str):
    cookies, imp = _login_ctx(sid)
    if not cookies:
        return JSONResponse({"error": "未登录"}, status_code=400)
    return await _sessions(cookies, imp)


def _prof_ctx(name):
    p = profiles.load(name) if name in profiles.list_profiles() else None
    return (p.cookies, p.impersonate) if p else (None, None)


@app.get("/api/profiles/{name}/account")
async def prof_account(name: str):
    p = profiles.load(name) if name in profiles.list_profiles() else None
    if p is None or not p.cookies:
        return JSONResponse({"error": "无 cookie"}, status_code=400)
    info = await login.fetch_user_info(p.cookies, impersonate=p.impersonate)
    if info and info.get("face") and p.face != info["face"]:
        p.face = info["face"]
        profiles.save(p)
    return {"info": info}


@app.get("/api/profiles/{name}/bound")
async def prof_bound(name: str):
    cookies, imp = _prof_ctx(name)
    if not cookies:
        return JSONResponse({"error": "无 cookie"}, status_code=400)
    return {"bound": await _bound(cookies, imp)}


@app.post("/api/profiles/{name}/bind")
async def prof_bind(name: str, body: dict):
    cookies, imp = _prof_ctx(name)
    if not cookies:
        return JSONResponse({"error": "无 cookie"}, status_code=400)
    return await _bind(cookies, imp, body)


@app.get("/api/profiles/{name}/sessions")
async def prof_sessions(name: str):
    cookies, imp = _prof_ctx(name)
    if not cookies:
        return JSONResponse({"error": "无 cookie"}, status_code=400)
    return await _sessions(cookies, imp)


@app.post("/api/profiles/{name}/proxy-check")
async def prof_proxy_check(name: str, body: dict | None = None):
    p = profiles.load(name) if name in profiles.list_profiles() else None
    if p is None:
        return JSONResponse({"error": "配置不存在或已损坏"}, status_code=404)
    if not p.proxies:
        return JSONResponse({"error": "没有配置代理"}, status_code=400)
    conc = (body or {}).get("concurrency") or settings_store.load().get("proxy_concurrency", 40)
    r = await proxy_chk.start(p.proxies, p.impersonate, profile_name=name, concurrency=conc)
    return r


@app.post("/api/proxy/check")
async def proxy_check(body: dict):
    return await proxy_chk.start(body.get("proxies") or [], body.get("impersonate", DEFAULT_IMPERSONATE))


@app.get("/api/proxy/{tid}")
def proxy_status(tid: str):
    return proxy_chk.status(tid)


@app.get("/api/grab")
def grab_list():
    return grab_mgr.list()


@app.post("/api/grab/start")
async def grab_start(body: dict):
    return await grab_mgr.start(body.get("profiles") or [])


@app.post("/api/grab/{gid}/stop")
async def grab_stop(gid: str):
    return await asyncio.to_thread(grab_mgr.stop, gid)


@app.websocket("/ws/grab/{gid}")
async def ws_grab(ws: WebSocket, gid: str):
    bad = (ws.client and not _loopback_ip(ws.client.host)) or not _local_host(ws.headers.get("host"))
    if (not _ALLOW_LAN and (bad or not _local_host(ws.headers.get("origin")))):
        await ws.close(code=1008)
        return
    if _ALLOW_LAN:
        origin = ws.headers.get("origin")
        if origin and _host_of(origin) != _host_of(ws.headers.get("host")):
            await ws.close(code=1008)
            return
    await ws.accept()
    try:
        while True:
            snap = grab_mgr.snapshot(gid)
            await ws.send_json(snap)
            if snap.get("state") == "gone":
                break
            await asyncio.sleep(0.3 if snap.get("state") == "running" else 2.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/api/settings")
def get_settings():
    return settings_store.load()


@app.post("/api/settings")
def post_settings(body: dict):
    settings_store.save(body or {})
    return {"ok": True}


@app.get("/api/music")
def list_music():
    try:
        return sorted(f.name for f in MUSIC.glob("*.mp3"))
    except Exception:
        return []


@app.post("/api/notify/test")
async def notify_test(body: dict):
    ok, msg = await notify.test_channel(settings_store.load(), body.get("kind", ""))
    return {"ok": ok, "message": msg}


def _collect_rids(obj, out: set) -> None:
    if isinstance(obj, dict):
        for k in ("inter_reserve_id", "reserve_id"):
            if obj.get(k) is not None:
                out.add(obj[k])
        for v in obj.values():
            _collect_rids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_rids(v, out)


@app.get("/api/profiles/{name}/tickets")
async def prof_tickets(name: str):
    cookies, imp = _prof_ctx(name)
    if not cookies:
        return JSONResponse({"error": "无 cookie"}, status_code=400)
    c = BwsClient(cookies, imp)
    try:
        opts = await collect_sessions(c)
        try:
            mr = await c.my_reserve()
        except Exception:
            mr = {}
    finally:
        await c.aclose()
    reserved: set = set()
    _collect_rids((mr or {}).get("data") or {}, reserved)
    sessions = [{"reserve_id": o["reserve_id"], "date": o["date"], "title": o["title"],
                 "type_name": o.get("type_name"), "location": o.get("location"),
                 "begin": o.get("begin"), "end": o.get("end"),
                 "act_begin": o.get("act_begin"), "act_end": o.get("act_end"),
                 "stock": o.get("stock"), "total": o.get("total"),
                 "ticket_no": o.get("ticket_no"), "reserved": o["reserve_id"] in reserved}
                for o in opts]
    return {"sessions": sessions}


if MUSIC.exists():
    app.mount("/music", StaticFiles(directory=str(MUSIC)), name="music")
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
