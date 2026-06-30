from __future__ import annotations

import asyncio
import struct
import time

_NTP_DELTA = 2208988800

DEFAULT_NTP_HOSTS = ["ntp.aliyun.com", "ntp1.aliyun.com", "cn.pool.ntp.org"]


async def query_ntp(host: str = "ntp.aliyun.com", port: int = 123,
                    timeout: float = 3.0) -> tuple[int, float]:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

    t0 = time.monotonic()
    transport, _ = await loop.create_datagram_endpoint(_Proto, remote_addr=(host, port))
    try:
        transport.sendto(b"\x1b" + 47 * b"\x00")
        data = await asyncio.wait_for(fut, timeout)
        t1 = time.monotonic()
    finally:
        transport.close()

    ms = _parse_ntp(data)
    if ms is None:
        raise ValueError("NTP 响应无效")
    return ms, (t0 + t1) / 2.0


def _parse_ntp(data: bytes) -> int | None:
    if len(data) < 48:
        return None
    b0 = data[0]
    li, mode, stratum = (b0 >> 6) & 0x3, b0 & 0x7, data[1]
    if li == 3 or stratum == 0 or mode != 4:
        return None
    secs, frac = struct.unpack("!II", data[40:48])
    if secs == 0:
        return None
    server_ms = int((secs - _NTP_DELTA) * 1000 + (frac / 2**32) * 1000)
    if not (1_500_000_000_000 <= server_ms <= 4_100_000_000_000):
        return None
    return server_ms


async def query_ntp_any(hosts: list[str] | None = None,
                        timeout: float = 3.0) -> tuple[int, float] | None:
    for host in (hosts or DEFAULT_NTP_HOSTS):
        try:
            return await query_ntp(host, timeout=timeout)
        except Exception:
            continue
    return None
