from __future__ import annotations

import asyncio
import time

from net.http import new_async_session, DEFAULT_IMPERSONATE
from core.login import get_csrf
from net.ntp import query_ntp_any

API = "https://api.bilibili.com/x/activity/bws/online/park"
YEAR = 202601
BID = 202601
ACT_DAYS = ["20260710", "20260711", "20260712"]

ID_TYPES = {
    0: "身份证",
    1: "护照",
    2: "港澳居民来往内地通行证",
    3: "台湾居民来往内地通行证",
}

CODE_SUCCESS = 0
STOP_CODES = {75574, 76647}
STOP_MESSAGES = {75574: "场次已被抢空", 76647: "预约数已达上限"}
RELIEF_CODES = {76651, 75637}
RISK_CODES = {-702, -412, -509}
FATAL_CODES = {-101, -111, -352, 75638, 75636}
FATAL_MESSAGES = {-101: "账号未登录", -111: "csrf 校验失败", -352: "触发风控校验(需人工/重登)",
                  75638: "未绑定门票信息", 75636: "实名信息校验不通过"}

BIND_MESSAGES = {
    0: "绑定成功",
    75636: "票务身份信息校验不通过",
    75642: "当前账号已经被绑定",
    75643: "当前证件下，未查询到购票信息",
    76645: "邀请函用户暂不支持门票认证",
    75638: "需先绑定门票信息",
}


class BwsClient:

    def __init__(self, cookies: list[dict], impersonate: str = DEFAULT_IMPERSONATE,
                 proxy: str | None = None):
        self.cookies = cookies
        self.csrf = get_csrf(cookies)
        self.session = new_async_session(impersonate, proxy=proxy, headers={
            "Origin": "https://www.bilibili.com",
            "Referer": "https://www.bilibili.com/blackboard/era/bws2026-event.html",
            "Accept": "application/json, text/plain, */*",
        })
        for c in cookies or []:
            name, value = c.get("name"), c.get("value")
            if name and value is not None:
                try:
                    self.session.cookies.set(name, value, domain=".bilibili.com")
                except Exception:
                    pass

    async def aclose(self) -> None:
        try:
            await self.session.close()
        except Exception:
            pass

    async def _get(self, path: str, params: dict | None = None, timeout: float = 10) -> dict:
        p = {"csrf": self.csrf, "year": YEAR}
        if params:
            p.update(params)
        r = await self.session.get(f"{API}{path}", params=p, timeout=timeout)
        return r.json()

    async def _post(self, path: str, data: dict, timeout: float = 8) -> dict:
        d = {"csrf": self.csrf, "year": YEAR}
        d.update(data)
        r = await self.session.post(f"{API}{path}", data=d, timeout=timeout)
        return r.json()

    async def ticket_check(self) -> dict:
        return await self._get("/ticket/check")

    async def is_bound(self) -> bool | None:
        try:
            return bool(((await self.ticket_check()).get("data") or {}).get("is_bind"))
        except Exception:
            return None

    async def ticket_bind(self, user_name: str, personal_id: str, ticket_no4: str,
                          id_type: int = 0) -> dict:
        return await self._post("/ticket/bind", {
            "user_name": user_name,
            "id_type": id_type,
            "personal_id": personal_id,
            "ticket_no": ticket_no4,
            "bid": BID,
        })

    async def reserve_info(self, reserve_type: int = 0, dates: list[str] | None = None) -> dict:
        return await self._get("/reserve/info", {
            "reserve_date": ",".join(dates or ACT_DAYS),
            "reserve_type": reserve_type,
        })

    async def my_reserve(self) -> dict:
        return await self._get("/myreserve")

    async def reserve_do(self, inter_reserve_id, ticket_no, timeout: float = 5) -> dict:
        url = f"{API}/reserve/do"
        d = {"csrf": self.csrf, "year": YEAR,
             "inter_reserve_id": inter_reserve_id, "ticket_no": ticket_no}
        try:
            r = await self.session.post(url, data=d, timeout=timeout)
            http = getattr(r, "status_code", None)
            try:
                body = r.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                return {"http": http, "code": body.get("code"),
                        "message": body.get("message", ""), "error": False}
            return {"http": http, "code": None, "message": "非JSON响应", "error": True}
        except Exception as e:
            return {"http": None, "code": None, "message": f"网络异常:{e}", "error": True}

    async def server_time(self, timeout: float = 5) -> float | None:
        try:
            data = (await self._get("/server/time", timeout=timeout)).get("data") or {}
            t = data.get("server_time")
            return float(t) if t is not None else None
        except Exception:
            return None


def classify_reserve(outcome: dict) -> str:
    code = outcome.get("code")
    http = outcome.get("http")
    if code == CODE_SUCCESS:
        return "success"
    if code in STOP_CODES:
        return "stop"
    if code in FATAL_CODES:
        return "fatal"
    if http in (412, 429, 403) or (http is not None and http >= 500) or code in RISK_CODES:
        return "risk"
    if code in RELIEF_CODES:
        return "relief"
    return "throttle"


def now_ms() -> int:
    return int(time.time() * 1000)


class ServerClock:

    def __init__(self, client: BwsClient, ntp_hosts: list[str] | None = None):
        self.client = client
        self.ntp_hosts = ntp_hosts
        self.anchor_mono = time.monotonic()
        self.base_ms = now_ms()
        self._a = (self.base_ms, self.anchor_mono)
        self.source = "local"
        self.synced = False
        self.bili_minus_ntp: int | None = None

    async def _sample_bili(self) -> tuple[int, float] | None:
        t0 = time.monotonic()
        t = await self.client.server_time(timeout=5)
        t1 = time.monotonic()
        if t is None:
            return None
        return (int(round(t * 1000)), (t0 + t1) / 2.0)

    async def sync(self) -> bool:
        ntp, bili = await asyncio.gather(
            query_ntp_any(self.ntp_hosts),
            self._sample_bili(),
            return_exceptions=True,
        )
        if isinstance(ntp, BaseException):
            ntp = None
        if isinstance(bili, BaseException):
            bili = None

        if ntp and bili:
            self.bili_minus_ntp = int((bili[0] - bili[1] * 1000) - (ntp[0] - ntp[1] * 1000))

        if ntp:
            self._anchor(ntp[0], ntp[1], "ntp")
        elif bili:
            self._anchor(bili[0], bili[1], "bili")
        else:
            self._anchor(now_ms(), time.monotonic(), "local")
            self.synced = False
            return False
        self.synced = True
        return True

    async def resync_ntp(self, guard=None) -> bool:
        r = await query_ntp_any(self.ntp_hosts)
        if r and (guard is None or guard()):
            self._anchor(r[0], r[1], "ntp")
            return True
        return False

    def _anchor(self, server_ms: int, mono_mid: float, source: str) -> None:
        self._a = (server_ms, mono_mid)
        self.base_ms, self.anchor_mono, self.source = server_ms, mono_mid, source

    def now_ms(self) -> int:
        base, mono = self._a
        return int(base + (time.monotonic() - mono) * 1000)

    def describe(self) -> str:
        return {"ntp": "已校时 NTP", "bili": "已校时 B站", "local": "未校时·本地墙钟"}.get(
            self.source, f"时间源 {self.source}")
