from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from net.http import new_async_session, DEFAULT_IMPERSONATE, IMPERSONATE_CHOICES
from paths import COOKIE_FILE

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASSPORT_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
PASSPORT_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"


def parse_cookie_list(cookie_str: str) -> list[dict]:
    cookies: list[dict] = []
    parts = cookie_str.split(",")
    merged: list[str] = []
    current = ""
    for part in parts:
        if "=" in part.split(";", 1)[0]:
            if current:
                merged.append(current.strip())
            current = part
        else:
            current += "," + part
    if current:
        merged.append(current.strip())
    for item in merged:
        key_value = item.split(";", 1)[0] if ";" in item else item
        if "=" in key_value:
            key, value = key_value.split("=", 1)
            cookies.append({"name": key.strip(), "value": value.strip()})
    return cookies


def parse_cookie_header(header: str) -> list[dict]:
    cookies: list[dict] = []
    for piece in header.strip().strip(";").split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        if name:
            cookies.append({"name": name, "value": value.strip()})
    return cookies


def cookies_to_header(cookies: list[dict]) -> str:
    return "; ".join(
        f"{c['name']}={c['value']}"
        for c in cookies
        if c.get("name") and c.get("value") is not None
    )


def get_cookie_value(cookies: list[dict], name: str) -> str | None:
    for c in cookies:
        if c.get("name") == name:
            return c.get("value")
    return None


def get_csrf(cookies: list[dict]) -> str:
    return get_cookie_value(cookies, "bili_jct") or ""


def save_cookies(cookies: list[dict], path: Path | str = COOKIE_FILE, *,
                 uid: str = "", uname: str = "") -> None:
    data = {"cookies": cookies, "uid": uid, "uname": uname, "updated_at": int(time.time())}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cookies(path: Path | str = COOKIE_FILE) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        cks = data.get("cookies")
        return cks if isinstance(cks, list) else []
    if isinstance(data, list):
        return data
    return []


def _jar_to_list(session, resp=None) -> list[dict]:
    merged: dict[str, str] = {}
    try:
        for ck in session.cookies.jar:
            if ck.name and ck.value is not None:
                merged[ck.name] = ck.value
    except Exception:
        pass
    if resp is not None and "SESSDATA" not in merged:
        try:
            for c in parse_cookie_list(resp.headers.get("set-cookie", "") or ""):
                merged.setdefault(c["name"], c["value"])
        except Exception:
            pass
    return [{"name": k, "value": v} for k, v in merged.items()]


async def fetch_user_info(cookies: list[dict], *,
                          impersonate: str = DEFAULT_IMPERSONATE,
                          proxy: str | None = None) -> dict | None:
    if not cookies:
        return None
    headers = {"referer": "https://www.bilibili.com/", "cookie": cookies_to_header(cookies)}
    try:
        async with new_async_session(impersonate, headers=headers, proxy=proxy) as s:
            r = await s.get(NAV_API, timeout=10)
            data = r.json().get("data") or {}
    except Exception:
        return None
    uname = data.get("uname")
    if not (data.get("isLogin") and uname):
        return None
    return {
        "uid": str(data.get("mid", "")),
        "uname": str(uname),
        "face": str(data.get("face", "")),
        "is_vip": data.get("vipStatus", 0) == 1,
        "level": (data.get("level_info") or {}).get("current_level", 0),
        "coins": data.get("money", 0),
    }


def render_qr(url: str, *, compact: bool = False, fit: tuple[int, int] | None = None) -> str:
    import qrcode

    qr = qrcode.QRCode(
        border=1 if compact else 2,
        error_correction=qrcode.constants.ERROR_CORRECT_L if compact else qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    if fit and n:
        cols, rows = fit
        scale = max(1, min(cols // n, (rows * 2) // n))
        if scale > 1:
            up = []
            for srow in matrix:
                wide = [v for v in srow for _ in range(scale)]
                for _ in range(scale):
                    up.append(list(wide))
            matrix = up
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))

    lines: list[str] = []
    for r in range(0, len(matrix), 2):
        row = []
        for c in range(len(matrix[0])):
            top, bot = matrix[r][c], matrix[r + 1][c]
            if not top and not bot:
                row.append("█")
            elif not top and bot:
                row.append("▀")
            elif top and not bot:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


def print_qr(url: str) -> None:
    print()
    print(render_qr(url))
    print()
    print("用「哔哩哔哩」手机 App 扫描上方二维码并确认登录")
    print(f"   若终端二维码无法扫描,可复制此链接用其它工具生成:\n   {url}")
    print()


async def _warmup_session(session) -> None:
    try:
        await session.get("https://www.bilibili.com/", timeout=10)
    except Exception:
        pass
    try:
        r = await session.get(SPI_API, timeout=10)
        spi = r.json().get("data") or {}
        for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
            val = spi.get(key)
            if val:
                session.cookies.set(name, val, domain=".bilibili.com")
    except Exception:
        pass


async def generate_qrcode(session, max_retry: int = 10) -> tuple[str, str]:
    last = "二维码生成失败"
    for _ in range(max_retry):
        try:
            r = await session.get(PASSPORT_GENERATE, timeout=10)
            payload = r.json()
        except Exception as e:
            last = str(e)
            await asyncio.sleep(1)
            continue
        if payload.get("code") == 0:
            d = payload["data"]
            return d["url"], d["qrcode_key"]
        last = payload.get("message", last)
        await asyncio.sleep(1)
    raise RuntimeError(f"生成二维码失败:{last}")


async def poll_qrcode(session, qrcode_key: str, *,
                      timeout: float = 180.0, interval: float = 1.5) -> list[dict] | None:
    deadline = time.time() + timeout
    hinted = {86101: False, 86090: False}
    while time.time() < deadline:
        try:
            resp = await session.get(PASSPORT_POLL, params={"qrcode_key": qrcode_key}, timeout=10)
            payload = resp.json()
        except Exception:
            await asyncio.sleep(interval)
            continue

        if payload.get("code") != 0:
            await asyncio.sleep(interval)
            continue

        data = payload.get("data", {})
        state = data.get("code")

        if state == 0:
            return _jar_to_list(session, resp)
        if state == 86101:
            if not hinted[86101]:
                print("   等待扫码...")
                hinted[86101] = True
        elif state == 86090:
            if not hinted[86090]:
                print("   已扫描,请在手机上点击确认登录...")
                hinted[86090] = True
        elif state == 86038:
            print("   二维码已失效,请重试")
            return None
        else:
            print(f"   登录失败:{data.get('message', state)}")
            return None
        await asyncio.sleep(interval)

    print("   ⏰ 登录超时")
    return None


async def poll_qrcode_cb(session, qrcode_key: str, on_state=None, *,
                         timeout: float = 180.0, interval: float = 1.5) -> list[dict] | None:
    def emit(s):
        if on_state:
            on_state(s)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            resp = await session.get(PASSPORT_POLL, params={"qrcode_key": qrcode_key}, timeout=10)
            payload = resp.json()
        except Exception:
            await asyncio.sleep(interval)
            continue
        if payload.get("code") != 0:
            await asyncio.sleep(interval)
            continue
        state = (payload.get("data") or {}).get("code")
        if state == 0:
            return _jar_to_list(session, resp)
        if state == 86038:
            emit("expired")
            return None
        if state == 86101:
            if last != "waiting":
                emit("waiting"); last = "waiting"
        elif state == 86090:
            if last != "scanned":
                emit("scanned"); last = "scanned"
        else:
            emit("error")
            return None
        await asyncio.sleep(interval)
    emit("timeout")
    return None


async def qr_login(*, timeout: float = 180.0,
                   impersonate: str = DEFAULT_IMPERSONATE,
                   proxy: str | None = None) -> list[dict] | None:
    async with new_async_session(impersonate, proxy=proxy) as session:
        print(f"正在准备登录环境...(指纹:{impersonate})")
        await _warmup_session(session)
        url, key = await generate_qrcode(session)
        print_qr(url)
        cookies = await poll_qrcode(session, key, timeout=timeout)
    if not cookies:
        return None
    info = await fetch_user_info(cookies, impersonate=impersonate, proxy=proxy)
    if not info:
        print("拿到 cookie,但登录态校验失败(仍会保存,请用 --check 复核)")
        save_cookies(cookies)
        return cookies
    print(f"登录成功:{info['uname']} (uid={info['uid']})")
    save_cookies(cookies, uid=info["uid"], uname=info["uname"])
    print(f"   cookie 已保存到 {COOKIE_FILE}")
    return cookies


async def manual_login(*, impersonate: str = DEFAULT_IMPERSONATE,
                       proxy: str | None = None) -> list[dict] | None:
    print("\n请粘贴浏览器里的 Cookie 字符串:")
    print("(获取方式:F12 → Network → 刷新 → 任意请求 → Request Headers → Cookie,整行复制)")
    raw = input("> ").strip()
    if not raw:
        print("未输入,取消。")
        return None
    cookies = parse_cookie_header(raw)
    if not cookies:
        print("解析不到任何 cookie。")
        return None
    info = await fetch_user_info(cookies, impersonate=impersonate, proxy=proxy)
    if not info:
        print("这串 cookie 校验登录态失败(可能缺少 SESSDATA 或已过期)。")
        return None
    print(f"登录成功:{info['uname']} (uid={info['uid']})")
    save_cookies(cookies, uid=info["uid"], uname=info["uname"])
    print(f"   cookie 已保存到 {COOKIE_FILE}")
    return cookies


async def ensure_login(*, force: bool = False,
                       impersonate: str = DEFAULT_IMPERSONATE,
                       proxy: str | None = None) -> list[dict]:
    if not force:
        saved = load_cookies()
        if saved:
            info = await fetch_user_info(saved, impersonate=impersonate, proxy=proxy)
            if info:
                print(f"复用已保存的登录:{info['uname']} (uid={info['uid']})")
                return saved
            print("本地 cookie 已失效,需要重新登录。")
    return await interactive_login(impersonate=impersonate, proxy=proxy)


async def interactive_login(*, impersonate: str = DEFAULT_IMPERSONATE,
                            proxy: str | None = None) -> list[dict]:
    while True:
        print("\n请选择登录方式:")
        print("  1) 扫码登录(推荐,默认)")
        print("  2) 手动粘贴 Cookie")
        print("  q) 退出")
        choice = input("> ").strip().lower()
        if choice in ("1", ""):
            cookies = await qr_login(impersonate=impersonate, proxy=proxy)
        elif choice == "2":
            cookies = await manual_login(impersonate=impersonate, proxy=proxy)
        elif choice == "q":
            raise SystemExit("已取消登录。")
        else:
            print("无效选择。")
            continue
        if cookies:
            return cookies
        print("登录未成功,再试一次。")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="B站登录,获取并保存 cookie")
    parser.add_argument("--force", action="store_true", help="忽略本地 cookie,强制重新登录")
    parser.add_argument("--check", action="store_true", help="只检查本地 cookie 是否有效")
    parser.add_argument("--impersonate", choices=IMPERSONATE_CHOICES, default=DEFAULT_IMPERSONATE,
                        help="移动端指纹模拟目标")
    args = parser.parse_args()

    async def run():
        if args.check:
            info = await fetch_user_info(load_cookies(), impersonate=args.impersonate)
            print(f"已登录:{info['uname']} (uid={info['uid']})" if info else "未登录或 cookie 已失效")
            return
        cookies = await ensure_login(force=args.force, impersonate=args.impersonate)
        print(f"\ncsrf_token (bili_jct) = {get_csrf(cookies)}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
