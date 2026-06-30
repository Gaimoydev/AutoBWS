from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque

from core import login
from core.api import BwsClient, ServerClock
from core.grabber import jobs_from_profile, ThreadedGrab
from core.lock import acquire_accounts, release_all
from net.http import new_async_session
from net.proxy import resolve_pool
from net import proxycheck


def _tok() -> str:
    return secrets.token_hex(8)


def _console(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


_BG_TASKS: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


class LoginManager:
    def __init__(self):
        self.sessions: dict[str, dict] = {}

    async def start(self, impersonate: str) -> dict:
        now = time.time()
        for k in [k for k, v in self.sessions.items() if now - v.get("ts", now) > 600]:
            self.sessions.pop(k, None)
        sid = _tok()
        sess = new_async_session(impersonate)
        try:
            await login._warmup_session(sess)
            url, key = await login.generate_qrcode(sess)
        except Exception as e:
            try:
                await sess.close()
            except Exception:
                pass
            return {"error": f"二维码生成失败:{e}"}
        st = {"id": sid, "impersonate": impersonate, "session": sess, "key": key, "url": url,
              "state": "waiting", "cookies": None, "info": None, "ts": now}
        self.sessions[sid] = st
        _spawn(self._poll(st))
        return {"id": sid, "url": url}

    async def _poll(self, st: dict) -> None:
        def on_state(s):
            if st["state"] not in ("success", "error"):
                st["state"] = s
        try:
            cookies = await login.poll_qrcode_cb(st["session"], st["key"], on_state=on_state)
            if cookies:
                info = await login.fetch_user_info(cookies, impersonate=st["impersonate"])
                if info:
                    st["cookies"], st["info"], st["state"] = cookies, info, "success"
                    _console(f"[登录] 成功 {info['uname']} (uid {info['uid']})")
                else:
                    st["state"] = "error"
        except Exception:
            st["state"] = "error"
        finally:
            try:
                await st["session"].close()
            except Exception:
                pass

    def status(self, sid: str) -> dict:
        st = self.sessions.get(sid)
        if not st:
            return {"state": "gone"}
        return {"state": st["state"], "url": st["url"], "info": st["info"]}

    def cookies_of(self, sid: str):
        st = self.sessions.get(sid)
        return st["cookies"] if st else None


class ProxyChecker:
    def __init__(self):
        self.tasks: dict[str, dict] = {}

    async def start(self, proxies: list, impersonate: str, profile_name: str | None = None,
                    concurrency: int = 40) -> dict:
        raws = resolve_pool(proxies or [])
        if not raws:
            return {"error": "没有有效代理"}
        for k in list(self.tasks)[:-10]:
            self.tasks.pop(k, None)
        tid = _tok()
        self.tasks[tid] = {"done": 0, "total": len(raws), "state": "running",
                           "ranked": None, "saved": False}
        _spawn(self._run(tid, raws, impersonate, profile_name, max(1, int(concurrency or 40))))
        return {"id": tid, "total": len(raws)}

    async def _run(self, tid: str, raws: list, imp: str, profile_name: str | None, concurrency: int) -> None:
        t = self.tasks[tid]

        def prog(done, total, res):
            t["done"] = done
        try:
            res = await proxycheck.evaluate(raws, imp, timeout=3.0, concurrency=concurrency, on_progress=prog)
            ranked = list(res.get("ranked") or [])
            t["ranked"] = ranked
            if profile_name and ranked:
                from core import profiles as profmod
                p = profmod.load(profile_name) if profile_name in profmod.list_profiles() else None
                if p is not None:
                    p.proxies = ranked
                    profmod.save(p)
                    t["saved"] = True
            t["state"] = "done"
            _console(f"[代理检测] 完成 可用 {len(ranked)}/{len(raws)}")
        except Exception:
            t["state"] = "error"

    def status(self, tid: str) -> dict:
        t = self.tasks.get(tid)
        if not t:
            return {"state": "gone"}
        return {"state": t["state"], "done": t["done"], "total": t["total"],
                "available": len(t["ranked"]) if t["ranked"] is not None else None}


class GrabManager:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.finished: dict[str, dict] = {}

    async def start(self, profile_names: list[str]) -> dict:
        from core import profiles as profmod
        profs = profmod.load_all(profile_names)
        if not profs:
            return {"error": "找不到配置"}
        ok, skipped, locks = acquire_accounts(profs)
        if not ok:
            return {"error": "所选账号都已在其它任务中抢票", "skipped": skipped}

        try:
            account_opts = {}
            for p in ok:
                raws = resolve_pool(p.proxies)
                account_opts[p.name] = {"proxies": raws or [None],
                                        "fallback_direct": getattr(p, "fallback_direct", True)}
            jobs = []
            for p in ok:
                jobs += jobs_from_profile(p)
            if not jobs:
                release_all(locks)
                return {"error": "所选配置没有可抢的场次"}

            sc = BwsClient(ok[0].cookies, ok[0].impersonate)
            clock = ServerClock(sc)
            try:
                await clock.sync()
            finally:
                await sc.aclose()
            if getattr(clock, "source", None) == "local":
                _console("[警告] 未能校时(NTP/B站均失败),改用本地墙钟,开抢时刻可能有偏差")

            logbuf: deque = deque(maxlen=500)
            log_seq = [0]
            names_label = " · ".join(p.name for p in ok)

            def _notify(m):
                logbuf.append(m)
                log_seq[0] += 1
                _console(f"[抢票] {m}")

            tg = ThreadedGrab(jobs, clock, account_opts=account_opts, notify=_notify, refresh=True)
            tg.start()
        except Exception:
            release_all(locks)
            raise
        _console(f"[抢票] 开始 {names_label} · {len(jobs)} 场次 · {clock.describe()}")
        gid = _tok()
        self.jobs[gid] = {"id": gid, "tg": tg, "clock": clock, "locks": locks,
                          "log": logbuf, "log_seq": log_seq, "names": [p.name for p in ok],
                          "skipped": skipped, "started": time.time()}
        _spawn(self._supervise(gid))
        return {"id": gid, "names": [p.name for p in ok], "skipped": skipped,
                "clock": clock.describe()}

    async def _supervise(self, gid: str) -> None:
        from web import settings as settingsmod
        from core import notify
        st = settingsmod.load()
        seen, risk_alerted, fatal_alerted = set(), False, False
        while gid in self.jobs:
            j = self.jobs.get(gid)
            if not j:
                return
            tg = j["tg"]
            try:
                rows = list(tg.progress.values())
            except Exception:
                rows = []
            for r in rows:
                key = f"{r.get('account')}#{r.get('reserve_id')}"
                if r.get("ok") and key not in seen:
                    seen.add(key)
                    if st.get("notify_on_win"):
                        await notify.notify_all(st, "AUTOBWS 抢中",
                                                f"{r.get('account')} · {r.get('title')}（{r.get('date')}）抢中", "win")
            if st.get("notify_on_risk") and not risk_alerted:
                try:
                    risk = tg.stat_totals().get("risk", 0)
                except Exception:
                    risk = 0
                if risk >= 60:
                    risk_alerted = True
                    await notify.notify_all(st, "AUTOBWS 风控告警", "持续风控,建议检查代理或放宽间隔", "risk")
            if not fatal_alerted and any(r.get("phase") == "失效" for r in rows):
                fatal_alerted = True
                await notify.notify_all(st, "AUTOBWS 登录/绑定失效",
                                        "账号登录态或绑定失效,该账号已停,请重新扫码登录/绑定", "risk")
            if tg.all_done:
                won = sum(1 for r in tg.progress.values() if r.get("ok"))
                if st.get("notify_on_done"):
                    await notify.notify_all(st, "AUTOBWS 完成", f"全部完成,抢中 {won}/{len(tg.progress)}", "done")
                await self._finish(gid)
                return
            await asyncio.sleep(1.5)

    def _teardown(self, j: dict) -> None:
        try:
            j["tg"].stop(); j["tg"].join(timeout=3); j["tg"].close()
        except Exception:
            pass
        release_all(j["locks"])

    async def _finish(self, gid: str) -> None:
        j = self.jobs.get(gid)
        if not j:
            return
        snap = self.snapshot(gid)
        snap["state"] = "done"
        if self.jobs.pop(gid, None) is None:
            return
        await asyncio.to_thread(self._teardown, j)
        self.finished[gid] = {"snap": snap, "names": j["names"], "ts": time.time()}
        _console(f"[抢票] 结束 {' · '.join(j['names'])}")

    def snapshot(self, gid: str) -> dict:
        j = self.jobs.get(gid)
        if not j:
            fin = self.finished.get(gid)
            return dict(fin["snap"]) if fin else {"state": "gone"}
        tg, clock = j["tg"], j["clock"]
        try:
            rows = sorted(tg.progress.values(), key=lambda p: (p["account"], p["date"], p["reserve_id"] or 0))
            stats = dict(tg.stat_totals())
            log = list(j["log"])[-60:]
        except Exception:
            rows, stats, log = list(tg.progress.values()), {}, list(j["log"])[-60:]
        return {
            "state": "done" if tg.all_done else "running",
            "names": j["names"], "skipped": j["skipped"], "clock_desc": clock.describe(),
            "countdown_ms": max(0, tg.earliest_target - clock.now_ms()),
            "stats": stats,
            "rows": [{k: r.get(k) for k in ("account", "reserve_id", "title", "date", "begin", "proxy",
                                            "phase", "attempts", "interval", "code", "msg",
                                            "done", "ok", "result")} for r in rows],
            "log": log, "log_seq": j.get("log_seq", [0])[0],
        }

    def stop(self, gid: str) -> dict:
        j = self.jobs.pop(gid, None)
        if j:
            self._teardown(j)
            _console(f"[抢票] 停止 {' · '.join(j['names'])}")
        self.finished.pop(gid, None)
        return {"state": "stopped"}

    def _reap(self) -> None:
        now = time.time()
        for gid in [g for g, f in list(self.finished.items()) if now - f["ts"] > 1800]:
            self.finished.pop(gid, None)

    def list(self) -> list:
        self._reap()
        out = [{"id": gid, "names": j["names"], "done": False} for gid, j in list(self.jobs.items())]
        out += [{"id": gid, "names": f["names"], "done": True} for gid, f in list(self.finished.items())]
        return out

    def stop_all(self) -> None:
        for gid in list(self.jobs):
            self.stop(gid)
        self.finished.clear()


class LivenessMonitor:

    def __init__(self, interval: int = 480):
        self.interval = interval
        self.status: dict[str, dict] = {}
        self._started = False

    def alive_of(self, name: str):
        s = self.status.get(name)
        return s["alive"] if s else None

    def start(self) -> None:
        if not self._started:
            self._started = True
            _spawn(self._loop())

    async def _loop(self) -> None:
        from core import profiles as profmod, login
        while True:
            try:
                for n in list(profmod.list_profiles()):
                    p = profmod.load(n)
                    if not (p and p.cookies):
                        continue
                    info = await login.fetch_user_info(p.cookies, impersonate=p.impersonate)
                    prev = self.status.get(n, {}).get("alive")
                    self.status[n] = {"alive": info is not None, "ts": time.time()}
                    if info is None and prev is not False:
                        _console(f"[测活] 登录失效 {n},请重新登录")
                    if info and info.get("face") and p.face != info["face"]:
                        p.face = info["face"]
                        profmod.save(p)
            except Exception:
                pass
            await asyncio.sleep(self.interval)
