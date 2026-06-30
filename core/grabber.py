from __future__ import annotations

import asyncio
import copy
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from core.api import BwsClient, ServerClock, classify_reserve, STOP_MESSAGES, FATAL_MESSAGES
from core.profiles import _PACE_POLICY_DEFAULT, _as_int
from net.proxy import proxy_label
from utils.fmt import set_timer_resolution

FALLBACK_GRAB_WINDOW_MS = 10 * 60 * 1000
MAX_ATTEMPTS = 200_000
SWITCH_THRESHOLD = 12
NET_FAIL_THRESHOLD = 3


async def collect_sessions(client: BwsClient, notify: Callable[[str], None] | None = None) -> list[dict]:
    def _say(m):
        if notify:
            notify(m)
    options: list[dict] = []
    for rtype, tname in ((0, "活动场次"), (1, "商品场次")):
        try:
            resp = await client.reserve_info(reserve_type=rtype)
        except Exception as e:
            _say(f"拉取{tname}失败: {e}")
            continue
        if resp.get("code") != 0:
            _say(f"{tname}: [{resp.get('code')}] {resp.get('message')}")
            continue
        data = resp.get("data") or {}
        reserve_list = data.get("reserve_list") or {}
        ticket_info = data.get("user_ticket_info") or {}
        for date, sessions in reserve_list.items():
            if isinstance(sessions, dict):
                sessions = [sessions]
            ticket_no = (ticket_info.get(date) or {}).get("ticket")
            for s in (sessions or []):
                if not isinstance(s, dict):
                    continue
                options.append({
                    "type": rtype, "type_name": tname, "date": date,
                    "reserve_id": s.get("reserve_id"),
                    "title": s.get("act_title") or s.get("sku_name") or "(无标题)",
                    "location": s.get("reserve_location") or "",
                    "begin": s.get("reserve_begin_time") or 0,
                    "end": s.get("reserve_end_time") or 0,
                    "act_begin": s.get("act_begin_time") or 0,
                    "act_end": s.get("act_end_time") or 0,
                    "stock": s.get("standard_stock", 0),
                    "total": s.get("standard_ticket_num", 0),
                    "state": s.get("state"),
                    "ticket_no": ticket_no,
                })
    options.sort(key=lambda o: (o["date"], o["begin"], o["reserve_id"] or 0))
    return options


def selectable(o: dict) -> bool:
    return bool(o.get("ticket_no")) and o.get("reserve_id") is not None


STOP_SCOPES = ("session", "daytype", "account")


def resolve_stop(cat: str, code, policy: dict) -> tuple[str | None, str]:
    """决定某次结果的终止动作。

    返回 (scope, pace_cat):
      scope    None=不终止 | "session" | "daytype" | "account"
      pace_cat scope is None 时用于选择节奏分支(normal/relief/risk/throttle)。
    """
    if cat == "success":
        sc = policy.get("success", "session")
        return (sc if sc in STOP_SCOPES else "session"), cat
    if code == 75574:
        pol = policy.get("soldout", "session")
        return (pol, cat) if pol in STOP_SCOPES else (None, "normal")
    if code == 76647:
        pol = policy.get("limit", "daytype")
        return (pol, cat) if pol in STOP_SCOPES else (None, "normal")
    if cat == "stop":
        return "session", cat
    return None, cat


@dataclass
class AccountStop:
    event: asyncio.Event
    buckets: set

    def stopped(self, date, typ) -> bool:
        return self.event.is_set() or (date, typ) in self.buckets


@dataclass
class GrabJob:
    key: str
    account: str
    cookies: list
    impersonate: str
    sess: dict
    base_ms: int
    offset_ms: int
    stop_policy: dict = field(default_factory=lambda: {"success": "session", "soldout": "session", "limit": "daytype"})
    pace_policy: dict = field(default_factory=lambda: copy.deepcopy(_PACE_POLICY_DEFAULT))


def jobs_from_profile(profile) -> list[GrabJob]:
    out = []
    for s in (profile.sessions or []):
        if not s.get("reserve_id") or not s.get("ticket_no"):
            continue
        out.append(GrabJob(key=f"{profile.name}#{s['reserve_id']}", account=profile.name,
                           cookies=profile.cookies, impersonate=profile.impersonate,
                           sess=dict(s), base_ms=profile.base_interval, offset_ms=profile.offset,
                           stop_policy=dict(getattr(profile, "stop_policy", None) or {}),
                           pace_policy=copy.deepcopy(getattr(profile, "pace_policy", None) or {})))
    return out


def make_progress(job: GrabJob) -> dict:
    return {
        "account": job.account, "reserve_id": job.sess.get("reserve_id"),
        "title": job.sess["title"], "date": job.sess["date"],
        "begin": job.sess["begin"], "proxy": "直连",
        "phase": "等待", "attempts": 0, "interval": 0,
        "code": None, "msg": "", "done": False, "ok": False, "result": "",
    }


class AccountPacer:

    def __init__(self, base_ms: int, policy: dict | None = None):
        pol = policy or _PACE_POLICY_DEFAULT
        self.base = max(1, base_ms)
        self.max = max(self.base, _as_int(pol.get("max_ms"), 1500))
        self.jitter = max(0, _as_int(pol.get("jitter_ms"), 40))
        self.relief = min(self.max, max(1, _as_int(pol.get("relief_ms"), 120)))
        self.curve = pol.get("curve", "accel")
        self.thr = pol.get("throttle") or {"mode": "auto", "value": self.base}
        self.rsk = pol.get("risk") or {"mode": "auto", "value": 800}
        self.risk_floor = max(self.base, _as_int((self.rsk or {}).get("value"), self.base))
        self.interval = float(self.base)
        self.streak = 0
        self.risk_cool = 0
        self.lock = asyncio.Lock()
        self.next_ok = 0.0
        self.last_fire = 0.0

    def _slot(self) -> float:
        # 抖动只加在退避节奏上;最热的 relief 重试不加(单边抖动只会平白增延迟)
        jit = random.uniform(0, self.jitter / 1000.0) if self.interval > self.relief else 0.0
        return self.last_fire + self.interval / 1000.0 + jit

    async def gate(self) -> None:
        # 在锁内只确定开火时刻并预占下个槽(用当前 interval 维持错峰),锁外再 sleep。
        async with self.lock:
            now = time.monotonic()
            self.last_fire = max(now, self.next_ok)
            wait = self.last_fire - now
            self.next_ok = self._slot()
        if wait > 0:
            await asyncio.sleep(wait)

    def rearm(self) -> None:
        # 分类响应后按最新 interval、以本次开火时刻为锚重排下个槽(修正"慢一拍")。
        self.next_ok = self._slot()

    @property
    def at_max(self) -> bool:
        return self.interval >= self.max

    def _backoff(self, cfg: dict) -> float:
        if cfg.get("mode") == "fixed":
            return float(min(self.max, max(self.base, _as_int(cfg.get("value"), self.base))))
        floor = max(self.base, _as_int(cfg.get("value"), self.base))
        step = self.base if self.curve == "linear" else self.base * self.streak
        return float(min(self.max, max(self.interval, floor) + step))

    def on_relief(self) -> None:
        floor = self.relief
        if self.risk_cool > 0:                 # 风控冷却期内,relief 不得砸破风控地板
            floor = max(self.relief, self.risk_floor)
            self.risk_cool -= 1
        self.streak = 0
        self.interval = float(floor)

    def on_throttle(self) -> None:
        self.streak += 1
        self.interval = self._backoff(self.thr)

    def on_risk(self) -> None:
        self.streak += 1
        self.risk_cool = 2
        self.interval = self._backoff(self.rsk)

    def on_neterr(self) -> None:
        # 传输错误:小幅、有界(≤2*base),不动 streak,不像服务器节流那样加速退避
        self.interval = float(min(self.max, max(self.interval, 2 * self.base)))

    def reset(self) -> None:
        # 换代理后退避从头来
        self.streak = 0
        self.interval = float(self.base)


class AccountCtx:

    def __init__(self, proxies: list, *, fallback_direct: bool = True,
                 switch_threshold: int = SWITCH_THRESHOLD,
                 net_fail_threshold: int = NET_FAIL_THRESHOLD):
        pool = [p for p in (proxies or []) if p]
        if fallback_direct or not pool:
            pool = pool + [None]
        self.pool = pool
        self.idx = 0
        self.gen = 0
        self.maxed = 0
        self.net_fail = 0
        self.threshold = switch_threshold
        self.net_threshold = net_fail_threshold

    @property
    def proxy(self):
        return self.pool[self.idx]

    @property
    def label(self) -> str:
        p = self.pool[self.idx]
        return proxy_label(p) if p else "直连"

    @property
    def has_alt(self) -> bool:
        return len(self.pool) > 1

    def report(self, *, hard_throttle: bool = False, at_max: bool = False,
               net_error: bool = False, good: bool = False) -> None:
        # 失败累积、互不清零(交替失败也能攒够切换信号);仅"好响应"衰减
        if good:
            self.net_fail = max(0, self.net_fail - 1)
            self.maxed = max(0, self.maxed - 1)
            return
        if net_error:
            self.net_fail += 1
        if hard_throttle and at_max:
            self.maxed += 1

    def reason_to_switch(self) -> str | None:
        if not self.has_alt:
            return None
        if self.net_fail >= self.net_threshold:
            return "代理失效"
        if self.maxed >= self.threshold:
            return "持续风控"
        return None

    def switch(self) -> str:
        self.idx = (self.idx + 1) % len(self.pool)
        self.gen += 1
        self.maxed = 0
        self.net_fail = 0
        return self.label


async def _probe_proxy(proxy: str, impersonate: str, timeout: float = 3.0) -> bool:
    from net.http import new_async_session
    try:
        async with new_async_session(impersonate, proxy=proxy) as s:
            r = await s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=timeout)
        return getattr(r, "status_code", 0) == 200
    except Exception:
        return False


async def _bg_close(client) -> None:
    try:
        await client.aclose()
    except Exception:
        pass


def effective_offset(rtt_ms, lo: int = 10, hi: int = 1000, margin: int = 15) -> int:
    # 让首包正好压在开闸:到达≈开火+单向延迟(≈RTT/2),故提前量≈RTT/2,再加小余量略早到
    return max(lo, min(hi, int(round(rtt_ms / 2 + margin))))


def _job_offset_ms(job) -> int:
    return 50 if job.offset_ms == "auto" else int(job.offset_ms)


async def _measure_offset(client, n: int = 3) -> tuple[int, float | None]:
    rtts = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            await client.server_time(timeout=1)
        except Exception:
            continue
        rtts.append((time.monotonic() - t0) * 1000)
    if not rtts:
        return 50, None
    rtt = min(rtts)
    return effective_offset(rtt), round(rtt)


async def grab_one(job: GrabJob, clock: ServerClock, ctx: AccountCtx, pacer: AccountPacer, *,
                   stop_event: threading.Event, progress: dict, stats: Counter,
                   astop: AccountStop, notify: Callable[[str], None] | None = None) -> None:
    p = progress[job.key]
    sess = job.sess
    date, typ = sess.get("date"), sess.get("type")

    def say(m: str) -> None:
        if notify:
            notify(m)

    cur_gen = ctx.gen
    p["proxy"] = ctx.label
    client = BwsClient(job.cookies, job.impersonate, ctx.proxy)
    bg: list = []

    def swap(old):
        new = BwsClient(job.cookies, job.impersonate, ctx.proxy)
        t = asyncio.create_task(_bg_close(old))
        bg.append(t)
        t.add_done_callback(lambda tt: bg.remove(tt) if tt in bg else None)
        p["proxy"] = ctx.label
        pacer.reset()                     # 换代理 → 退避从头来,别背着旧代理的退避包袱
        return new

    try:
        auto_off = (job.offset_ms == "auto")
        off = 50 if auto_off else int(job.offset_ms)
        target_ms = sess["begin"] * 1000 - off
        end_ms = sess["end"] * 1000 if sess["end"] else 0
        deadline_ms = end_ms if end_ms else target_ms + FALLBACK_GRAB_WINDOW_MS

        p["phase"] = "蹲点"
        warmed = False
        while not stop_event.is_set() and not astop.stopped(date, typ):
            remaining = target_ms - clock.now_ms()
            if remaining <= 0:
                break
            if not warmed and remaining < 3000:
                if auto_off:
                    off, rtt = await _measure_offset(client)   # 测 RTT,顺带热连接
                    target_ms = sess["begin"] * 1000 - off
                    deadline_ms = end_ms if end_ms else target_ms + FALLBACK_GRAB_WINDOW_MS
                    if rtt is not None:
                        say(f"{job.account} · {sess['title'][:12]} 自动提前 {off}ms(RTT {round(rtt)}ms)")
                else:
                    try:
                        await client.server_time(timeout=1)
                    except Exception:
                        pass
                warmed = True
            await asyncio.sleep(min((remaining - 50) / 1000.0, 0.2) if remaining > 60 else 0.002)
        if stop_event.is_set() or astop.stopped(date, typ):
            return

        attempts = 0
        while not stop_event.is_set() and not astop.stopped(date, typ):
            if ctx.gen != cur_gen:
                client = swap(client)
                cur_gen = ctx.gen
                continue
            if clock.now_ms() > deadline_ms:
                p.update(phase="截止", done=True, result="截止/超时")
                return
            if attempts >= MAX_ATTEMPTS:
                p.update(phase="停止", done=True, result="达发包上限")
                return

            await pacer.gate()
            if stop_event.is_set() or astop.stopped(date, typ):
                break
            attempts += 1
            outcome = await client.reserve_do(sess["reserve_id"], sess["ticket_no"])
            net_error = bool(outcome.get("error")) and outcome.get("http") is None
            cat = classify_reserve(outcome)
            code = outcome.get("code")
            scope, pace_cat = resolve_stop(cat, code, job.stop_policy)
            stats["sent"] += 1
            stats[cat] += 1
            if net_error:
                stats["net"] += 1
            p.update(attempts=attempts, code=code, msg=outcome.get("message") or "", interval=int(pacer.interval))

            if attempts == 1:
                p["phase"] = "开抢"
                say("开抢")

            if cat == "fatal":
                msg = FATAL_MESSAGES.get(code, outcome.get("message") or f"HTTP {outcome.get('http')}")
                p.update(phase="失效", done=True, result=f"已停止:{msg}")
                if not astop.event.is_set():
                    astop.event.set()
                    say(f"{job.account}:{msg} —— 已停该账号,请重新扫码登录/绑定")
                return

            if scope is not None:
                if cat == "success":
                    stats["win"] += 1
                    p.update(phase="抢中", done=True, ok=True, result="已抢中")
                    say(f"{job.account} · {sess['title'][:12]} 抢中")
                else:
                    msg = STOP_MESSAGES.get(code, outcome.get("message"))
                    p.update(phase="停止", done=True, result=f"已停止:{msg}")
                    say(f"{job.account} · {sess['title'][:12]}:{msg}")
                if scope == "daytype":
                    if (date, typ) not in astop.buckets:
                        astop.buckets.add((date, typ))
                        say(f"{job.account} 触发同日同类停止({date}/{sess.get('type_name','')})")
                elif scope == "account":
                    if not astop.event.is_set():
                        astop.event.set()
                        say(f"{job.account} 触发账号停止")
                return

            if net_error:
                pacer.on_neterr()
                ctx.report(hard_throttle=False, at_max=pacer.at_max, net_error=True)
                p["phase"] = "网络重试"
            elif pace_cat == "normal":
                p["phase"] = "抢票中"          # 不暂停:中性继续,不触碰账号级共享 pacer/ctx
            elif pace_cat == "relief":
                pacer.on_relief()
                ctx.report(good=True)          # 拥挤=通道在开,视为好响应,衰减切换计数
                p["phase"] = "抢票中"
            elif pace_cat == "risk":
                pacer.on_risk()
                ctx.report(hard_throttle=True, at_max=True, net_error=False)
                p["phase"] = "退避"
            else:
                pacer.on_throttle()
                ctx.report(hard_throttle=(not net_error), at_max=pacer.at_max, net_error=net_error)
                p["phase"] = "退避"
            pacer.rearm()   # 按最新 interval 重排下个槽(修正限速慢一拍)

            if ctx.gen == cur_gen:
                reason = ctx.reason_to_switch()
                if reason:
                    newlabel = ctx.switch()
                    say(f"切换代理 {job.account}（{reason}）→ {newlabel}")
                    client = swap(client)
                    cur_gen = ctx.gen
    except Exception as e:
        p.update(phase="异常", result="网络/内部异常")
        say(f"异常 {job.account} · {sess['title'][:12]}:{type(e).__name__}")
    finally:
        if not p["done"]:
            if astop.event.is_set():
                res = "已中止(账号停止)"
            elif (date, typ) in astop.buckets:
                res = "已中止(同日同类停止)"
            else:
                res = p.get("result") or "已中止"
            phase = p["phase"] if p["phase"] in ("停止", "截止", "异常") else "已中止"
            p.update(done=True, phase=phase, result=res)
        await _bg_close(client)
        if bg:
            await asyncio.gather(*list(bg), return_exceptions=True)


async def _refresh_account(jobs: list[GrabJob], ctx: AccountCtx, clock: ServerClock,
                           progress: dict, notify: Callable[[str], None] | None = None) -> None:
    if not jobs:
        return
    earliest = min(j.sess["begin"] * 1000 - _job_offset_ms(j) for j in jobs)
    if earliest - clock.now_ms() < 25_000:
        return
    try:
        client = BwsClient(jobs[0].cookies, jobs[0].impersonate, ctx.proxy)
        try:
            opts = await collect_sessions(client)
        finally:
            await client.aclose()
        by_id = {o["reserve_id"]: o for o in opts}
        for j in jobs:
            f = by_id.get(j.sess.get("reserve_id"))
            if not f:
                continue
            for k in ("begin", "end", "ticket_no", "stock", "total", "title", "location"):
                if f.get(k) is not None:
                    j.sess[k] = f[k]
            p = progress[j.key]
            p["title"] = j.sess["title"]
            p["date"] = j.sess["date"]
            p["begin"] = j.sess["begin"]
    except Exception:
        if notify:
            notify(f"刷新场次失败,用快照（{jobs[0].account}）")


class ThreadedGrab:

    def __init__(self, jobs: list[GrabJob], clock: ServerClock,
                 account_opts: dict[str, dict] | None = None,
                 notify: Callable[[str], None] | None = None, refresh: bool = True,
                 switch_threshold: int = SWITCH_THRESHOLD):
        self.clock = clock
        self.jobs = jobs
        self.refresh = refresh
        self.notify = notify
        self.progress: dict = {j.key: make_progress(j) for j in jobs}
        self.by_account: dict[str, list[GrabJob]] = {}
        for j in jobs:
            self.by_account.setdefault(j.account, []).append(j)
        account_opts = account_opts or {}

        def _ctx(acct: str) -> AccountCtx:
            opt = account_opts.get(acct) or {}
            return AccountCtx(opt.get("proxies") or [None],
                              fallback_direct=opt.get("fallback_direct", True),
                              switch_threshold=switch_threshold)
        self.ctxs: dict[str, AccountCtx] = {acct: _ctx(acct) for acct in self.by_account}
        self.stats: dict[str, Counter] = {acct: Counter() for acct in self.by_account}
        for j in jobs:
            self.progress[j.key]["proxy"] = self.ctxs[j.account].label
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.earliest_target = (min(j.sess["begin"] * 1000 - _job_offset_ms(j) for j in jobs)
                                if jobs else 0)

    def start(self) -> None:
        set_timer_resolution(True)
        threading.Thread(target=self._resync_loop, daemon=True, name="clock-resync").start()
        for account, ajobs in self.by_account.items():
            t = threading.Thread(target=self._account_thread, args=(account, ajobs),
                                 daemon=True, name=f"acct-{account}")
            t.start()
            self.threads.append(t)

    def _resync_loop(self) -> None:
        while not self.stop_event.wait(30):
            if self.earliest_target - self.clock.now_ms() <= 20_000:
                continue
            try:
                asyncio.run(self.clock.resync_ntp(
                    guard=lambda: self.earliest_target - self.clock.now_ms() > 10_000))
            except Exception:
                pass

    def _account_thread(self, account: str, ajobs: list[GrabJob]) -> None:
        try:
            asyncio.run(self._account_async(account, ajobs))
        except Exception as e:
            for j in ajobs:
                pp = self.progress.get(j.key)
                if pp and not pp["done"]:
                    pp.update(done=True, phase="异常", result=f"线程异常:{type(e).__name__}")
            if self.notify:
                self.notify(f"账号线程异常（{account}）:{type(e).__name__}")

    async def _account_async(self, account: str, ajobs: list[GrabJob]) -> None:
        ctx = self.ctxs[account]
        pacer = AccountPacer(ajobs[0].base_ms if ajobs else 80,
                             ajobs[0].pace_policy if ajobs else None)
        stats = self.stats[account]
        astop = AccountStop(event=asyncio.Event(), buckets=set())
        if self.refresh:
            await _refresh_account(ajobs, ctx, self.clock, self.progress, self.notify)
            if self.jobs:
                self.earliest_target = min(j.sess["begin"] * 1000 - _job_offset_ms(j) for j in self.jobs)
        tasks = [grab_one(j, self.clock, ctx, pacer, stop_event=self.stop_event,
                          progress=self.progress, stats=stats, astop=astop, notify=self.notify) for j in ajobs]
        if ctx.has_alt:
            tasks.append(self._liveness(account, ajobs, ctx, astop))
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._reconcile(account, ajobs, ctx)

    async def _liveness(self, account: str, ajobs: list[GrabJob], ctx: AccountCtx, astop: AccountStop) -> None:
        keys = [j.key for j in ajobs]
        imp = ajobs[0].impersonate
        fails = 0
        while not self.stop_event.is_set() and not astop.event.is_set():
            if all(self.progress[k]["done"] for k in keys):
                return
            slept = 0.0
            while slept < 15 and not self.stop_event.is_set() and not astop.event.is_set():
                await asyncio.sleep(0.5)
                slept += 0.5
            if self.stop_event.is_set() or astop.event.is_set() or all(self.progress[k]["done"] for k in keys):
                return
            gen0, proxy0 = ctx.gen, ctx.proxy
            if proxy0 is None:
                continue
            ok = await _probe_proxy(proxy0, imp)
            if ctx.gen != gen0:
                fails = 0
                continue
            if ok:
                fails = 0
                continue
            fails += 1
            if fails >= 2 and ctx.has_alt:
                ctx.switch()
                fails = 0
                if self.notify:
                    self.notify(f"切换代理 {account}(测活失效)→ {ctx.label}")

    async def _reconcile(self, account: str, ajobs: list[GrabJob], ctx: AccountCtx) -> None:
        # 中签核对:抓完后,对"未中"的场次查 myreserve(本账号已预约的场次列表,按 reserve_id)。
        # 命中说明其实已抢到 —— 翻正"丢 ack / 响应截断把真中签记成 76647/没中"。按 reserve_id
        # 逐场次匹配(state==4 是单场次级信号,不是账号级),真·达上限不会被误翻。
        pending = [j for j in ajobs if not self.progress[j.key].get("ok")]
        if not pending:
            return
        try:
            client = BwsClient(ajobs[0].cookies, ajobs[0].impersonate, ctx.proxy)
            try:
                resp = await client.my_reserve()
            finally:
                await client.aclose()
        except Exception:
            return
        if not isinstance(resp, dict) or resp.get("code") != 0:
            return
        reserved = set()
        for sessions in ((resp.get("data") or {}).get("reserve_list") or {}).values():
            if isinstance(sessions, dict):
                sessions = [sessions]
            for s in (sessions or []):
                if isinstance(s, dict) and s.get("reserve_id") is not None:
                    reserved.add(s.get("reserve_id"))
        stats = self.stats[account]
        for j in pending:
            if j.sess.get("reserve_id") in reserved:
                self.progress[j.key].update(ok=True, done=True, phase="抢中", result="已抢中(核对确认)")
                stats["win"] += 1
                if self.notify:
                    self.notify(f"{account} · {j.sess.get('title', '')[:12]} 核对确认已抢中")

    def stat_totals(self) -> dict:
        total: Counter = Counter()
        for c in self.stats.values():
            for _ in range(3):
                try:
                    total.update(dict(c))
                    break
                except RuntimeError:
                    continue
        return dict(total)

    @property
    def all_done(self) -> bool:
        return all(p["done"] for p in self.progress.values())

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float = 3) -> None:
        for t in self.threads:
            t.join(timeout=timeout)

    def close(self) -> None:
        set_timer_resolution(False)
