# 偵測輔助重連(Discovery-Assisted Reconnect)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 LocWarp 的三條自動重連路徑(啟動自動連線、後端 watchdog、前端釘選重試)在 iPhone 換埠號/換 IP 後仍能自動連回,不再需要手動偵測。

**Architecture:** 後端把 mDNS/掃描偵測邏輯抽成 `services/tunnel_discovery.py` 供端點與 watchdog 共用;watchdog 直連重試失敗後依序做「同 IP 埠掃描 → 完整偵測」後備,連上後驗證 UDID。前端把候選合併與 savedips 操作抽成純函式(可測試),啟動自動連線在有釘選時也跑偵測、連上後用真實 UDID 把關。

**Tech Stack:** Python 3.13 / FastAPI / pymobiledevice3(後端);React 19 + TypeScript + Vite(前端);pytest + pytest-asyncio、vitest(新增測試基礎設施)。

**Spec:** `docs/superpowers/specs/2026-07-30-reconnect-discovery-design.md`

## Global Constraints

- Python 3.13(執行指令一律 `py -3.13`,倉庫根目錄為 `D:\locwarp`)
- 分支:`feat/reconnect-discovery`(已存在,直接在上面 commit)
- Commit 訊息:conventional commits(`feat:`/`fix:`/`test:`/`refactor:`),不加任何 attribution footer
- 偵測後備每層各跑一次、有界,失敗即走現有 teardown;不得無限循環
- UDID 不符一律「斷開 + 試下一候選」;但若連上的裝置本身在釘選清單內(家人另一支),保留該連線
- 不改 tunnel 傳輸協定、不做常駐週期掃描、不動群組同步/DDI 子系統
- 後端所有新程式碼不得讓 watchdog 協程因未捕捉例外而死亡

---

### Task 1: 後端 pytest 基礎設施

**Files:**
- Create: `backend/requirements-dev.txt`
- Create: `backend/pytest.ini`
- Create: `backend/tests/test_smoke.py`

**Interfaces:**
- Produces: `cd backend && py -3.13 -m pytest` 可執行;後續任務的測試都放在 `backend/tests/`。

- [ ] **Step 1: 建立 dev 依賴與 pytest 設定**

`backend/requirements-dev.txt`:

```
pytest>=8.0
pytest-asyncio>=0.24
```

`backend/pytest.ini`:

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 2: 安裝 dev 依賴**

Run: `py -3.13 -m pip install -r backend/requirements-dev.txt`

- [ ] **Step 3: 寫 smoke test**

`backend/tests/test_smoke.py`:

```python
"""Smoke test: pytest infrastructure works and backend modules import."""


def test_config_imports() -> None:
    import config

    assert hasattr(config, "RECONNECT_BASE_DELAY")


async def test_asyncio_mode_auto_works() -> None:
    import asyncio

    await asyncio.sleep(0)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `cd backend && py -3.13 -m pytest -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/requirements-dev.txt backend/pytest.ini backend/tests/test_smoke.py
git commit -m "test: 建立後端 pytest 基礎設施"
```

---

### Task 2: 抽出 `services/tunnel_discovery.py`(偵測邏輯重構)

**Files:**
- Create: `backend/services/tunnel_discovery.py`
- Modify: `backend/api/device.py`(移除搬走的函式,改為 import;`/wifi/tunnel/discover` 端點改為委派)
- Test: `backend/tests/test_tunnel_discovery.py`

**Interfaces:**
- Consumes: 無(第一個後端功能任務)。
- Produces:
  - `services.tunnel_discovery._get_primary_local_ip() -> str | None`
  - `services.tunnel_discovery._tcp_probe(ip: str, port: int, timeout: float = 0.4) -> bool`(async)
  - `services.tunnel_discovery._scan_subnet_for_port(port: int = 49152) -> list[str]`(async)
  - `services.tunnel_discovery._scan_ports_for_ip(ip, start=49152, end=65535, concurrency=1024, timeout=0.35) -> list[int]`(async)
  - `services.tunnel_discovery.discover_tunnel_candidates(*, browse=None, subnet_scan=None, port_scan=None) -> list[dict]`(async;dict 形如 `{"ip", "port", "host", "name", "method"}`,以 (ip, port) 去重)

**背景:** `backend/api/device.py`(1301 行)裡 `_get_primary_local_ip`(282 行)、`_tcp_probe`(296 行)、`_scan_subnet_for_port`(311 行)、`_scan_ports_for_ip`(331 行)與 `/wifi/tunnel/discover` 端點(385–493 行)的掃描邏輯要讓 watchdog 重用,搬到獨立模組。

- [ ] **Step 1: 寫失敗測試(針對 `discover_tunnel_candidates` 的可注入行為)**

`backend/tests/test_tunnel_discovery.py`:

```python
"""Tests for services.tunnel_discovery."""

from services.tunnel_discovery import discover_tunnel_candidates


async def test_mdns_results_returned_and_deduped() -> None:
    async def fake_browse() -> list[dict]:
        return [
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.10", "port": 50000, "host": "a", "name": "iPhone A", "method": "mdns"},
            {"ip": "192.168.1.11", "port": 50001, "host": "b", "name": "iPhone B", "method": "mdns"},
        ]

    result = await discover_tunnel_candidates(browse=fake_browse)
    assert [(r["ip"], r["port"]) for r in result] == [
        ("192.168.1.10", 50000),
        ("192.168.1.11", 50001),
    ]


async def test_mdns_empty_falls_back_to_subnet_scan() -> None:
    async def fake_browse() -> list[dict]:
        return []

    async def fake_subnet_scan(port: int) -> list[str]:
        return ["192.168.1.20"] if port == 49152 else []

    async def fake_port_scan(ip: str) -> list[int]:
        assert ip == "192.168.1.20"
        return [51234, 62078]

    result = await discover_tunnel_candidates(
        browse=fake_browse, subnet_scan=fake_subnet_scan, port_scan=fake_port_scan,
    )
    assert result == [
        {"ip": "192.168.1.20", "port": 51234, "host": "192.168.1.20",
         "name": "192.168.1.20", "method": "tcp_scan"},
    ]


async def test_browse_exception_still_falls_back() -> None:
    async def bad_browse() -> list[dict]:
        raise RuntimeError("mdns broken")

    async def fake_subnet_scan(port: int) -> list[str]:
        return []

    result = await discover_tunnel_candidates(
        browse=bad_browse, subnet_scan=fake_subnet_scan,
    )
    assert result == []
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `cd backend && py -3.13 -m pytest tests/test_tunnel_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.tunnel_discovery'`

- [ ] **Step 3: 建立 `backend/services/tunnel_discovery.py`**

把 `api/device.py` 的 `_get_primary_local_ip`、`_tcp_probe`、`_scan_subnet_for_port`、`_scan_ports_for_ip` **原封搬入**(含 docstring),再加上從 discover 端點抽出的主函式。模組骨架:

```python
"""WiFi tunnel candidate discovery (mDNS + subnet/port scanning).

Extracted from api/device.py so the /wifi/tunnel/discover endpoint and
the tunnel watchdog's reconnect fallback share one implementation.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("wifi_tunnel")


# _get_primary_local_ip / _tcp_probe / _scan_subnet_for_port /
# _scan_ports_for_ip: 從 api/device.py 282-362 行原封搬入,內容不變。


async def _browse_mdns() -> list[dict]:
    """mDNS / Bonjour RemotePairing broadcast → candidate dicts."""
    from pymobiledevice3.bonjour import browse_remotepairing

    results: list[dict] = []
    instances = await browse_remotepairing(timeout=3.0)
    for inst in instances:
        raw_addrs = inst.addresses or []
        str_addrs: list[str] = []
        for a in raw_addrs:
            if hasattr(a, "ip"):
                str_addrs.append(str(a.ip))
            else:
                str_addrs.append(str(a))
        ipv4s = [s for s in str_addrs if ":" not in s]
        addrs = ipv4s if ipv4s else str_addrs
        for addr in addrs:
            results.append({
                "ip": addr,
                "port": inst.port,
                "host": inst.host,
                "name": inst.instance or inst.host,
                "method": "mdns",
            })
    return results


async def discover_tunnel_candidates(
    *,
    browse=None,
    subnet_scan=None,
    port_scan=None,
) -> list[dict]:
    """Find iPhones on the local network. First tries mDNS; if that yields
    nothing, falls back to the smart /24 scan (probe 49152 + 62078, then
    full-range port scan per live host). Deduped on (ip, port).

    The browse / subnet_scan / port_scan hooks exist for tests only.
    """
    browse = browse or _browse_mdns
    subnet_scan = subnet_scan or _scan_subnet_for_port
    port_scan = port_scan or _scan_ports_for_ip
    results: list[dict] = []

    try:
        results.extend(await browse())
    except Exception as e:
        logger.warning("mDNS browse failed: %s", e)

    if not results:
        logger.info("mDNS empty; falling back to smart /24 scan (probe + full-range)")
        try:
            candidates: set[str] = set()
            for p in (49152, 62078):
                try:
                    candidates.update(await subnet_scan(p))
                except Exception as e:
                    logger.warning("probe scan port %d failed: %s", p, e)

            if candidates:
                logger.info(
                    "Smart scan found %d live host(s); full-range scanning each",
                    len(candidates),
                )

                async def _scan_one(ip: str) -> tuple[str, list[int]]:
                    try:
                        return ip, await port_scan(ip)
                    except Exception as e:
                        logger.warning("port scan for %s failed: %s", ip, e)
                        return ip, []

                scan_results = await asyncio.gather(*[_scan_one(ip) for ip in candidates])
                for ip, ports in scan_results:
                    if not ports:
                        continue
                    results.append({
                        "ip": ip, "port": ports[0], "host": ip,
                        "name": ip, "method": "tcp_scan",
                    })
        except Exception as e:
            logger.warning("Smart fallback scan failed: %s", e)

    seen: set[tuple] = set()
    unique: list[dict] = []
    for r in results:
        key = (r["ip"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique
```

注意:`_scan_subnet_for_port` 內呼叫的 `_get_primary_local_ip` 與 `_tcp_probe` 同檔即可取用;`_browse_mdns` 的內容即原端點 mDNS 區塊(`api/device.py` 393–418 行)原樣搬入。

- [ ] **Step 4: 改 `backend/api/device.py` 委派**

1. 刪除 282–362 行的四個函式(`_get_primary_local_ip`、`_tcp_probe`、`_scan_subnet_for_port`、`_scan_ports_for_ip`)。
2. 檔頭 import 區加:`from services.tunnel_discovery import _scan_ports_for_ip, discover_tunnel_candidates`(`/wifi/tunnel/find_port` 端點 379 行用到 `_scan_ports_for_ip`,名稱不變)。
3. `/wifi/tunnel/discover` 端點(385–493 行)整個函式體換成:

```python
@router.get("/wifi/tunnel/discover")
async def wifi_tunnel_discover():
    """Find iPhones on the local network. First tries mDNS (Bonjour RemotePairing
    broadcast); if that yields nothing, falls back to a smart /24 subnet scan."""
    return {"devices": await discover_tunnel_candidates()}
```

- [ ] **Step 5: 執行測試與 import 檢查**

Run: `cd backend && py -3.13 -m pytest -v && py -3.13 -c "import api.device; import services.tunnel_discovery; print('imports OK')"`
Expected: 全部 passed + `imports OK`

- [ ] **Step 6: Commit**

```bash
git add backend/services/tunnel_discovery.py backend/api/device.py backend/tests/test_tunnel_discovery.py
git commit -m "refactor: 抽出 tunnel 偵測邏輯至 services/tunnel_discovery 供 watchdog 重用"
```

---

### Task 3: `find_fallback_endpoints`(後備端點探索,TDD)

**Files:**
- Modify: `backend/services/tunnel_discovery.py`(檔尾新增函式)
- Test: `backend/tests/test_tunnel_discovery.py`(追加測試)

**Interfaces:**
- Consumes: Task 2 的 `_scan_ports_for_ip`、`discover_tunnel_candidates`。
- Produces: `services.tunnel_discovery.find_fallback_endpoints(ip: str | None, *, port_scan=None, discover=None) -> list[tuple[str, int]]`(async;順序=同 IP 埠掃描結果在前、discover 結果在後,(ip, port) 去重,單層失敗容忍)。

- [ ] **Step 1: 寫失敗測試**

追加到 `backend/tests/test_tunnel_discovery.py`:

```python
from services.tunnel_discovery import find_fallback_endpoints


async def test_fallback_prefers_same_ip_ports_then_discover() -> None:
    async def fake_port_scan(ip: str) -> list[int]:
        assert ip == "192.168.1.109"
        return [50100, 50200]

    async def fake_discover() -> list[dict]:
        return [
            {"ip": "192.168.1.109", "port": 50100},  # duplicate of port-scan hit
            {"ip": "192.168.1.50", "port": 51000},
        ]

    result = await find_fallback_endpoints(
        "192.168.1.109", port_scan=fake_port_scan, discover=fake_discover,
    )
    assert result == [
        ("192.168.1.109", 50100),
        ("192.168.1.109", 50200),
        ("192.168.1.50", 51000),
    ]


async def test_fallback_without_ip_uses_discover_only() -> None:
    async def fake_discover() -> list[dict]:
        return [{"ip": "192.168.1.50", "port": 51000}]

    called = False

    async def fake_port_scan(ip: str) -> list[int]:
        nonlocal called
        called = True
        return []

    result = await find_fallback_endpoints(
        None, port_scan=fake_port_scan, discover=fake_discover,
    )
    assert result == [("192.168.1.50", 51000)]
    assert called is False


async def test_fallback_tolerates_phase_failures() -> None:
    async def bad_port_scan(ip: str) -> list[int]:
        raise OSError("scan blew up")

    async def bad_discover() -> list[dict]:
        raise RuntimeError("discover blew up")

    result = await find_fallback_endpoints(
        "192.168.1.109", port_scan=bad_port_scan, discover=bad_discover,
    )
    assert result == []
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `cd backend && py -3.13 -m pytest tests/test_tunnel_discovery.py -v`
Expected: 新增 3 個測試 FAIL — `ImportError: cannot import name 'find_fallback_endpoints'`

- [ ] **Step 3: 實作**

`backend/services/tunnel_discovery.py` 檔尾新增:

```python
async def find_fallback_endpoints(
    ip: str | None,
    *,
    port_scan=None,
    discover=None,
) -> list[tuple[str, int]]:
    """Ordered candidate endpoints to try after direct reconnects failed.

    1. Every open dynamic-range port on the last-known IP — cheap (a few
       seconds), covers the common case where the iPhone rebound its
       RemotePairing port after a reboot / WiFi rejoin.
    2. Full discover results — covers a DHCP address change.

    Deduped on (ip, port); each phase tolerates failure independently.
    """
    port_scan = port_scan or _scan_ports_for_ip
    discover = discover or discover_tunnel_candidates
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    if ip:
        try:
            for p in await port_scan(ip):
                key = (ip, int(p))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        except Exception:
            logger.warning("Fallback port scan failed for %s", ip, exc_info=True)

    try:
        for cand in await discover():
            key = (str(cand["ip"]), int(cand["port"]))
            if key not in seen:
                seen.add(key)
                out.append(key)
    except Exception:
        logger.warning("Fallback discover failed", exc_info=True)

    return out
```

- [ ] **Step 4: 執行測試確認通過**

Run: `cd backend && py -3.13 -m pytest tests/test_tunnel_discovery.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/services/tunnel_discovery.py backend/tests/test_tunnel_discovery.py
git commit -m "feat: 新增 find_fallback_endpoints 後備端點探索"
```

---

### Task 4: watchdog 偵測後備 + UDID 驗證 + `tunnel_recovered` 帶端點

**Files:**
- Modify: `backend/api/device.py`
  - `_attempt_tunnel_restart`(607–743 行):UDID 驗證 + broadcast payload
  - `_per_tunnel_watchdog`(746–878 行):直連重試耗盡後的偵測後備

**Interfaces:**
- Consumes: Task 3 的 `find_fallback_endpoints(ip) -> list[tuple[str, int]]`。
- Produces: `tunnel_recovered` WebSocket broadcast payload 新增 `"ip"`(目標 iPhone 的區網 IP)與 `"port"`(RemotePairing 埠)兩個欄位 — Task 10 前端靠這兩個欄位更新 savedips。

**注意:** 此任務的核心是 asyncio 協調邏輯,依賴 `main.app_state` 與真實 TunnelRunner,單元測試成本極高;正確性靠 Task 3 已測的候選函式 + 本任務的小 diff + Task 11 手動驗證把關。

- [ ] **Step 1: `_attempt_tunnel_restart` 加 UDID 驗證**

在 `dev_info = await dm.connect_wifi_tunnel(new_rsd_address, new_rsd_port)`(661 行)之後、`app_state.simulation_engines.pop(...)`(666 行)之前插入:

```python
        # Discovery-assisted fallback can hand us an endpoint that turned
        # out to be a DIFFERENT iPhone (family device on the same LAN).
        # The pair-record handshake usually rejects that first, but if it
        # does connect, verify identity and bail — the except path below
        # rolls back the runner we registered.
        if dev_info.udid != udid:
            _tunnel_logger.warning(
                "Tunnel restart for %s reached a different device (%s); disconnecting",
                udid, dev_info.udid,
            )
            try:
                await dm.disconnect(dev_info.udid)
            except Exception:
                _tunnel_logger.debug(
                    "Disconnect of mismatched device failed", exc_info=True,
                )
            raise RuntimeError(
                f"udid mismatch: expected {udid}, got {dev_info.udid}"
            )
```

- [ ] **Step 2: `tunnel_recovered` broadcast 加端點欄位**

708–714 行的 broadcast 改成(加 `ip`/`port` 兩行,其餘不動):

```python
            await broadcast("tunnel_recovered", {
                "udid": dev_info.udid,
                "rsd_address": new_rsd_address,
                "rsd_port": new_rsd_port,
                "ip": ip,
                "port": port,
            })
```

(`ip`、`port` 即 `_attempt_tunnel_restart` 的參數 — 本次實際使用的目標端點。)

- [ ] **Step 3: `_per_tunnel_watchdog` 加偵測後備**

現行結構(834–857 行):`for attempt, delay in enumerate(_TUNNEL_RESTART_BACKOFF, ...)` 迴圈,成功即 `return`,迴圈走完落入 859 行起的 teardown。在該 for 迴圈結束後(仍在 `else:` 分支內、teardown 之前)插入:

```python
            # Direct retries against the old endpoint are exhausted. The
            # iPhone likely rebound its RemotePairing port (reboot / WiFi
            # rejoin) or got a new DHCP lease — re-discover before giving
            # up. Each phase runs once; total added time is bounded by
            # one port scan + one discover pass.
            from services.tunnel_discovery import find_fallback_endpoints

            candidates: list[tuple[str, int]] = []
            try:
                candidates = await find_fallback_endpoints(ip)
            except Exception:
                _tunnel_logger.exception(
                    "Fallback endpoint discovery failed for %s", udid,
                )
            for cand_ip, cand_port in candidates:
                if _tunnels.get(udid) is not runner:
                    _tunnel_logger.info(
                        "Tunnel for %s no longer registered during fallback; aborting",
                        udid,
                    )
                    return
                if cand_ip == ip and cand_port == port:
                    continue  # the direct-retry loop already tried this exact endpoint
                _tunnel_logger.info(
                    "Fallback restart attempt for %s via %s:%d",
                    udid, cand_ip, cand_port,
                )
                ok = await _attempt_tunnel_restart(
                    udid, cand_ip, cand_port, snapshot, runner,
                )
                if ok:
                    return
```

- [ ] **Step 4: 執行既有測試與 import 檢查**

Run: `cd backend && py -3.13 -m pytest -v && py -3.13 -c "import api.device; print('imports OK')"`
Expected: 全部 passed + `imports OK`

- [ ] **Step 5: Commit**

```bash
git add backend/api/device.py
git commit -m "feat: watchdog 直連失敗後改跑埠掃描與 mDNS 偵測後備,並驗證 UDID"
```

---

### Task 5: usbmux 不可用退避(TDD)

**Files:**
- Modify: `backend/core/device_manager.py`(新增 `UsbmuxAvailability` + `discover_devices` 使用)
- Modify: `backend/main.py`(`_usbmux_presence_watchdog` 尊重退避)
- Test: `backend/tests/test_usbmux_backoff.py`

**Interfaces:**
- Produces:
  - `core.device_manager.UsbmuxAvailability`:`should_attempt(now: float) -> bool`、`record_failure(now: float) -> bool`(回傳「是否為新一次故障(該記 log)」)、`record_success() -> bool`(回傳「是否剛恢復」)
  - 模組單例 `core.device_manager.usbmux_availability`

**背景:** 本機沒裝 Apple Mobile Device Service 時,`discover_devices`(`device_manager.py:149-153`)每次呼叫都 `logger.exception`,前端每 3 秒輪詢 → 日誌灌爆。`main.py:_usbmux_presence_watchdog`(371–375 行)每秒也在戳同一個死埠。

- [ ] **Step 1: 寫失敗測試**

`backend/tests/test_usbmux_backoff.py`:

```python
"""Tests for core.device_manager.UsbmuxAvailability."""

from core.device_manager import UsbmuxAvailability


def test_fresh_failure_reports_once_and_backs_off() -> None:
    a = UsbmuxAvailability(cooldown=60.0)
    assert a.should_attempt(now=0.0) is True
    assert a.record_failure(now=0.0) is True    # fresh outage → log it
    assert a.should_attempt(now=30.0) is False  # inside cooldown
    assert a.should_attempt(now=60.0) is True   # cooldown elapsed
    assert a.record_failure(now=60.0) is False  # still down → stay quiet


def test_recovery_resets_state() -> None:
    a = UsbmuxAvailability(cooldown=60.0)
    a.record_failure(now=0.0)
    assert a.record_success() is True           # was down → recovered
    assert a.record_success() is False          # already up → quiet
    assert a.should_attempt(now=1.0) is True
    assert a.record_failure(now=1.0) is True    # next outage logs again
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `cd backend && py -3.13 -m pytest tests/test_usbmux_backoff.py -v`
Expected: FAIL — `ImportError: cannot import name 'UsbmuxAvailability'`

- [ ] **Step 3: 實作 `UsbmuxAvailability`**

`backend/core/device_manager.py`,放在 `DeviceManager` class 定義之前:

```python
class UsbmuxAvailability:
    """Debounce usbmuxd connection failures.

    On machines without Apple Mobile Device Service every USB poll fails;
    without this, discover_devices logs a full ERROR traceback every few
    seconds forever. State machine: first failure logs once and pauses
    usbmux attempts for `cooldown` seconds; recovery logs once.
    """

    def __init__(self, cooldown: float = 60.0) -> None:
        self.cooldown = cooldown
        self._down_until: float = 0.0
        self._was_down = False

    def should_attempt(self, now: float) -> bool:
        return now >= self._down_until

    def record_failure(self, now: float) -> bool:
        """Register a failed attempt. Returns True iff this is a fresh
        outage (caller should log it)."""
        self._down_until = now + self.cooldown
        fresh = not self._was_down
        self._was_down = True
        return fresh

    def record_success(self) -> bool:
        """Register a successful attempt. Returns True iff the service
        just recovered from an outage."""
        recovered = self._was_down
        self._was_down = False
        self._down_until = 0.0
        return recovered


usbmux_availability = UsbmuxAvailability()
```

- [ ] **Step 4: 執行測試確認通過**

Run: `cd backend && py -3.13 -m pytest tests/test_usbmux_backoff.py -v`
Expected: PASS

- [ ] **Step 5: `discover_devices` 套用退避**

`device_manager.py` 現行 149–153 行:

```python
        try:
            raw_devices = await list_devices()
        except Exception:
            logger.exception("Failed to list usbmux devices")
            return devices
```

改為:

```python
        now = time.monotonic()
        if not usbmux_availability.should_attempt(now):
            return devices
        try:
            raw_devices = await list_devices()
        except Exception as exc:
            if usbmux_availability.record_failure(time.monotonic()):
                logger.warning(
                    "usbmuxd unreachable (%s: %s) — USB discovery paused, "
                    "retrying every %.0fs. Is Apple Mobile Device Service "
                    "(iTunes / Apple Devices) installed and running?",
                    type(exc).__name__, exc, usbmux_availability.cooldown,
                )
            return devices
        if usbmux_availability.record_success():
            logger.info("usbmuxd reachable again — USB discovery resumed")
```

檔頭若尚未 `import time` 則補上。

- [ ] **Step 6: `_usbmux_presence_watchdog` 尊重退避**

`backend/main.py` 371–375 行:

```python
            try:
                raw = await list_devices()
            except Exception:
                logger.debug("usbmux list_devices failed in watchdog", exc_info=True)
                continue
```

改為:

```python
            from core.device_manager import usbmux_availability
            if not usbmux_availability.should_attempt(time.monotonic()):
                continue
            try:
                raw = await list_devices()
            except Exception:
                usbmux_availability.record_failure(time.monotonic())
                logger.debug("usbmux list_devices failed in watchdog", exc_info=True)
                continue
            usbmux_availability.record_success()
```

(`time` 已在該函式 334 行 import。)

- [ ] **Step 7: 全部測試 + import 檢查**

Run: `cd backend && py -3.13 -m pytest -v && py -3.13 -c "import main; print('imports OK')"`
Expected: 全部 passed + `imports OK`

- [ ] **Step 8: Commit**

```bash
git add backend/core/device_manager.py backend/main.py backend/tests/test_usbmux_backoff.py
git commit -m "fix: usbmuxd 不可用時退避 60 秒,不再每 3 秒灌 ERROR traceback"
```

---

### Task 6: DVT 座標日誌加 udid(殭屍推送可觀測性)

**Files:**
- Modify: `backend/services/location_service.py`(`__init__` 94–105 行、log 行 196 與 205)
- Modify: `backend/core/device_manager.py`(595–599 行建構處傳入 udid)

**Interfaces:**
- Produces: `DvtLocationService.__init__(..., udid: str | None = None)`;日誌格式 `DVT location set to (lat, lng) [udid]`。

**背景:** 多裝置同時推送時 `DVT location set` 無法區分是哪支裝置/引擎,無從判斷 watchdog 重啟後是否有殭屍任務仍在推舊座標(spec 附帶小修 2)。

- [ ] **Step 1: `__init__` 加參數**

`location_service.py` 94–105 行的 `__init__` 簽名加 `udid: str | None = None`,並在 body 加 `self._udid = udid`:

```python
    def __init__(
        self,
        dvt_provider: DvtProvider,
        lockdown=None,
        dvt_factory: Callable[[], Awaitable[DvtProvider]] | None = None,
        udid: str | None = None,
    ) -> None:
        self._dvt = dvt_provider
        self._lockdown = lockdown
        self._dvt_factory = dvt_factory
        self._udid = udid
        self._location_sim: LocationSimulation | None = None
        self._active = False
        self._reconnect_lock = asyncio.Lock()
```

- [ ] **Step 2: 兩處 log 行加 udid**

196 行改為:

```python
            logger.info("DVT location set to (%.6f, %.6f) [%s]", lat, lng, self._udid or "?")
```

205 行改為:

```python
            logger.info("DVT location set to (%.6f, %.6f) after reconnect [%s]", lat, lng, self._udid or "?")
```

- [ ] **Step 3: 建構處傳入 udid**

`device_manager.py` 595–599 行改為:

```python
            return DvtLocationService(
                dvt,
                lockdown=conn.lockdown,
                dvt_factory=_factory,
                udid=udid,
            )
```

(`udid` 於外層函式作用域可用 — 592 行的 `_factory` 已引用同名變數。)

- [ ] **Step 4: 測試 + import 檢查**

Run: `cd backend && py -3.13 -m pytest -v && py -3.13 -c "import core.device_manager, services.location_service; print('imports OK')"`
Expected: 全部 passed + `imports OK`

- [ ] **Step 5: Commit**

```bash
git add backend/services/location_service.py backend/core/device_manager.py
git commit -m "feat: DVT 座標日誌加上 udid,供多裝置與殭屍推送診斷"
```

---

### Task 7: 前端 vitest 基礎設施

**Files:**
- Modify: `frontend/package.json`(devDependencies + `test` script)
- Create: `frontend/src/utils/__tests__/smoke.test.ts`

**Interfaces:**
- Produces: `cd frontend && npm test` 可執行;後續前端測試放 `frontend/src/utils/__tests__/`。

- [ ] **Step 1: 安裝 vitest**

Run: `cd frontend && npm install -D vitest`

- [ ] **Step 2: 加 test script**

`frontend/package.json` 的 `scripts` 加一行:

```json
    "test": "vitest run"
```

- [ ] **Step 3: 寫 smoke test**

`frontend/src/utils/__tests__/smoke.test.ts`:

```typescript
import { describe, expect, test } from 'vitest'

describe('vitest infrastructure', () => {
  test('runs TypeScript tests', () => {
    const value: number = 1 + 1
    expect(value).toBe(2)
  })
})
```

- [ ] **Step 4: 執行確認通過**

Run: `cd frontend && npm test`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/utils/__tests__/smoke.test.ts
git commit -m "test: 建立前端 vitest 基礎設施"
```

---

### Task 8: 前端純函式 — savedips 操作與自動連線候選合併(TDD)

**Files:**
- Create: `frontend/src/utils/savedIps.ts`
- Create: `frontend/src/utils/autoConnect.ts`
- Test: `frontend/src/utils/__tests__/savedIps.test.ts`
- Test: `frontend/src/utils/__tests__/autoConnect.test.ts`

**Interfaces:**
- Produces(Task 9、10 依賴這些簽名):

```typescript
// savedIps.ts
export interface SavedIpEntry {
  ip: string
  port: number
  udid?: string
  name?: string
  lastUsed: number
}
export const SAVED_IPS_KEY = 'locwarp.tunnel.savedips'
export function readSavedIps(storage?: Pick<Storage, 'getItem'>): SavedIpEntry[]
export function writeSavedIps(list: SavedIpEntry[], storage?: Pick<Storage, 'setItem'>): void
export function upsertSavedIp(list: SavedIpEntry[], entry: SavedIpEntry): SavedIpEntry[]  // 純函式
export function removeSavedIpByUdid(list: SavedIpEntry[], udid: string): SavedIpEntry[]   // 純函式

// autoConnect.ts
export interface TunnelCandidate { ip: string; port: number; udid?: string }
export function buildAutoConnectCandidates(opts: {
  saved: TunnelCandidate[]
  discovered: Array<{ ip: string; port: number }>
  pinnedUdids: string[]
  alreadyTunneled: ReadonlySet<string>   // "ip:port" keys
  max?: number                            // default 3
}): TunnelCandidate[]
```

- [ ] **Step 1: 寫失敗測試 — savedIps**

`frontend/src/utils/__tests__/savedIps.test.ts`:

```typescript
import { describe, expect, test } from 'vitest'
import { removeSavedIpByUdid, upsertSavedIp, type SavedIpEntry } from '../savedIps'

const entry = (over: Partial<SavedIpEntry>): SavedIpEntry => ({
  ip: '192.168.1.10', port: 50000, udid: 'U1', name: 'iPhone', lastUsed: 100, ...over,
})

describe('upsertSavedIp', () => {
  test('prepends new entry and dedups by udid (device moved to new endpoint)', () => {
    const list = [entry({ ip: '192.168.1.10', port: 50000, udid: 'U1' })]
    const next = upsertSavedIp(list, entry({ ip: '192.168.1.10', port: 51111, udid: 'U1', lastUsed: 200 }))
    expect(next).toHaveLength(1)
    expect(next[0].port).toBe(51111)
  })

  test('dedups by (ip, port) when udid differs', () => {
    const list = [entry({ udid: 'U1' })]
    const next = upsertSavedIp(list, entry({ udid: 'U2', lastUsed: 200 }))
    expect(next).toHaveLength(1)
    expect(next[0].udid).toBe('U2')
  })

  test('caps the ring buffer at 5 entries', () => {
    let list: SavedIpEntry[] = []
    for (let i = 0; i < 6; i++) {
      list = upsertSavedIp(list, entry({ ip: `192.168.1.${10 + i}`, udid: `U${i}`, lastUsed: i }))
    }
    expect(list).toHaveLength(5)
    expect(list[0].udid).toBe('U5')
  })

  test('does not mutate the input list', () => {
    const list = [entry({})]
    const snapshot = JSON.parse(JSON.stringify(list))
    upsertSavedIp(list, entry({ udid: 'U9', ip: '192.168.1.99' }))
    expect(list).toEqual(snapshot)
  })
})

describe('removeSavedIpByUdid', () => {
  test('removes matching udid, keeps others, does not mutate', () => {
    const list = [entry({ udid: 'U1' }), entry({ udid: 'U2', ip: '192.168.1.11' })]
    const next = removeSavedIpByUdid(list, 'U1')
    expect(next.map((e) => e.udid)).toEqual(['U2'])
    expect(list).toHaveLength(2)
  })
})
```

- [ ] **Step 2: 寫失敗測試 — autoConnect**

`frontend/src/utils/__tests__/autoConnect.test.ts`:

```typescript
import { describe, expect, test } from 'vitest'
import { buildAutoConnectCandidates } from '../autoConnect'

const noTunnels = new Set<string>()

describe('buildAutoConnectCandidates', () => {
  test('with pins: pinned saved entries first, then discovered endpoints', () => {
    const result = buildAutoConnectCandidates({
      saved: [
        { ip: '192.168.1.10', port: 50000, udid: 'PINNED' },
        { ip: '192.168.1.11', port: 50001, udid: 'OTHER' },
      ],
      discovered: [{ ip: '192.168.1.10', port: 61234 }],
      pinnedUdids: ['PINNED'],
      alreadyTunneled: noTunnels,
    })
    expect(result).toEqual([
      { ip: '192.168.1.10', port: 50000, udid: 'PINNED' },
      { ip: '192.168.1.10', port: 61234, udid: undefined },
    ])
  })

  test('without pins: keeps all saved entries plus discovered', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.11', port: 50001, udid: 'ANY' }],
      discovered: [{ ip: '192.168.1.12', port: 50002 }],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
    })
    expect(result.map((c) => c.ip)).toEqual(['192.168.1.11', '192.168.1.12'])
  })

  test('excludes endpoints already tunneled and dedups ip:port', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.10', port: 50000, udid: 'PINNED' }],
      discovered: [
        { ip: '192.168.1.10', port: 50000 },   // dup of saved
        { ip: '192.168.1.13', port: 50003 },   // already tunneled
      ],
      pinnedUdids: ['PINNED'],
      alreadyTunneled: new Set(['192.168.1.13:50003']),
    })
    expect(result).toEqual([{ ip: '192.168.1.10', port: 50000, udid: 'PINNED' }])
  })

  test('caps at max (default 3)', () => {
    const result = buildAutoConnectCandidates({
      saved: [],
      discovered: [
        { ip: '192.168.1.20', port: 1 }, { ip: '192.168.1.21', port: 2 },
        { ip: '192.168.1.22', port: 3 }, { ip: '192.168.1.23', port: 4 },
      ],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
    })
    expect(result).toHaveLength(3)
  })
})
```

- [ ] **Step 3: 執行測試確認失敗**

Run: `cd frontend && npm test`
Expected: FAIL — cannot resolve `../savedIps` / `../autoConnect`

- [ ] **Step 4: 實作 `savedIps.ts`**

```typescript
// Pure helpers around the locwarp.tunnel.savedips localStorage ring buffer
// (max 5 entries, newest first). Extracted so App.tsx / useDevice can share
// one implementation and the logic stays unit-testable.

export interface SavedIpEntry {
  ip: string
  port: number
  udid?: string
  name?: string
  lastUsed: number
}

export const SAVED_IPS_KEY = 'locwarp.tunnel.savedips'
const MAX_ENTRIES = 5

export function readSavedIps(storage: Pick<Storage, 'getItem'> = localStorage): SavedIpEntry[] {
  try {
    const parsed = JSON.parse(storage.getItem(SAVED_IPS_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((e) => e && typeof e.ip === 'string' && typeof e.port === 'number')
  } catch {
    return []
  }
}

export function writeSavedIps(list: SavedIpEntry[], storage: Pick<Storage, 'setItem'> = localStorage): void {
  try {
    storage.setItem(SAVED_IPS_KEY, JSON.stringify(list))
  } catch { /* storage disabled */ }
}

export function upsertSavedIp(list: SavedIpEntry[], entry: SavedIpEntry): SavedIpEntry[] {
  const filtered = list.filter((e) =>
    e
    && !(e.ip === entry.ip && e.port === entry.port)
    && !(entry.udid && e.udid === entry.udid),
  )
  return [entry, ...filtered].slice(0, MAX_ENTRIES)
}

export function removeSavedIpByUdid(list: SavedIpEntry[], udid: string): SavedIpEntry[] {
  return list.filter((e) => e.udid !== udid)
}
```

- [ ] **Step 5: 實作 `autoConnect.ts`**

```typescript
// Candidate list for launch auto-connect. With pins set we now still run
// discovery (the iPhone's RemotePairing port changes on every reboot /
// WiFi rejoin, so saved endpoints alone go stale) — identity is enforced
// AFTER connect by checking the handshake udid against the pin list.

export interface TunnelCandidate {
  ip: string
  port: number
  udid?: string
}

const DEFAULT_MAX = 3

export function buildAutoConnectCandidates(opts: {
  saved: TunnelCandidate[]
  discovered: Array<{ ip: string; port: number }>
  pinnedUdids: string[]
  alreadyTunneled: ReadonlySet<string>
  max?: number
}): TunnelCandidate[] {
  const { saved, discovered, pinnedUdids, alreadyTunneled } = opts
  const max = opts.max ?? DEFAULT_MAX
  const hasPins = pinnedUdids.length > 0
  const savedFiltered = hasPins
    ? saved.filter((e) => e.udid && pinnedUdids.includes(e.udid))
    : saved

  const seen = new Set<string>()
  const out: TunnelCandidate[] = []
  const add = (ip: string, port: number, udid?: string) => {
    const key = `${ip}:${port}`
    if (seen.has(key) || alreadyTunneled.has(key)) return
    seen.add(key)
    out.push({ ip, port, udid })
  }

  for (const e of savedFiltered) add(e.ip, e.port, e.udid)
  for (const d of discovered) add(d.ip, d.port, undefined)
  return out.slice(0, max)
}
```

- [ ] **Step 6: 執行測試確認通過**

Run: `cd frontend && npm test`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add frontend/src/utils/savedIps.ts frontend/src/utils/autoConnect.ts frontend/src/utils/__tests__/savedIps.test.ts frontend/src/utils/__tests__/autoConnect.test.ts
git commit -m "feat: savedips 純函式與自動連線候選合併邏輯(含測試)"
```

---

### Task 9: App.tsx 啟動自動連線改為「偵測 + 事後 UDID 驗證」

**Files:**
- Modify: `frontend/src/App.tsx`(389–503 行的 auto-connect effect;只動 426–495 行的候選蒐集與連線區塊)

**Interfaces:**
- Consumes: Task 8 的 `buildAutoConnectCandidates`、`readSavedIps`、`writeSavedIps`、`removeSavedIpByUdid`;`useDevice` 現有的 `device.startWifiTunnel(ip, port, udidHint?) -> Promise<DeviceInfo>`(回傳含 `udid`)與 `device.stopTunnel(udid)`。
- Produces: 無新介面(行為變更)。

- [ ] **Step 1: 改寫候選蒐集與連線區塊**

檔頭加 import:

```typescript
import { buildAutoConnectCandidates } from './utils/autoConnect'
import { readSavedIps, removeSavedIpByUdid, writeSavedIps } from './utils/savedIps'
```

`App.tsx` 445–495 行(從 `const seen = new Set<string>()` 到 `Promise.allSettled(...)` 結束)整段替換為:

```typescript
          // When the user has pinned devices we still want ONLY those
          // devices — but the old approach (skip discovery, replay stale
          // saved endpoints) meant a rebooted iPhone could never
          // auto-connect: RemotePairing rebinds its port on every boot.
          // New approach (see docs/superpowers/specs/
          // 2026-07-30-reconnect-discovery-design.md): always discover,
          // connect, then verify the handshake udid against the pin list
          // and immediately drop anything unpinned (issue #35 intent).
          const pinnedUdids: string[] = []
          try {
            const p = JSON.parse(localStorage.getItem('locwarp.tunnel.pinned') || '[]')
            if (Array.isArray(p)) pinnedUdids.push(...p.filter((x: any) => typeof x === 'string'))
          } catch { /* ignore */ }

          let discovered: Array<{ ip: string; port: number }> = []
          try {
            const dres = await api.wifiTunnelDiscover()
            discovered = (dres?.devices || []).map((d: any) => ({
              ip: String(d.ip),
              port: Number(d.port) || 49152,
            }))
          } catch { /* discover failed — saved entries still try */ }

          const candidates = buildAutoConnectCandidates({
            saved: savedList,
            discovered,
            pinnedUdids,
            alreadyTunneled,
            max: 3,
          })
          if (candidates.length === 0) return
          // Parallel: every iPhone gets a tunnel attempt at the same
          // time so the user doesn't wait sequentially for unreachable
          // ones to time out (~10s each). Pass entry.udid so the backend
          // tries the right pair record FIRST.
          await Promise.allSettled(
            candidates.map(async (entry) => {
              const info = await device.startWifiTunnel(entry.ip, entry.port, entry.udid).catch(() => null)
              if (!info) return
              if (pinnedUdids.length > 0 && !pinnedUdids.includes(info.udid)) {
                // Discovery reached a device the user never pinned —
                // undo the connect and scrub it from savedips so it
                // doesn't come back next launch.
                await device.stopTunnel(info.udid).catch(() => {})
                writeSavedIps(removeSavedIpByUdid(readSavedIps(), info.udid))
              }
            }),
          )
```

替換後,原本的 `addCand` helper、`hasPins` 分支、`filteredList`、`uniq`/`limited` 變數皆刪除;effect 前段(391–436 行:enabled 讀取、savedList 解析、`alreadyTunneled` 建立)保持不動。

- [ ] **Step 2: 型別檢查與建置**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: 無型別錯誤,測試全 PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: 啟動自動連線改為偵測+事後 UDID 驗證,釘選裝置換埠後可自動回連"
```

---

### Task 10: useDevice — 釘選重試加偵測後備、tunnel_recovered 更新 savedips

**Files:**
- Modify: `frontend/src/hooks/useDevice.ts`
  - `schedulePinReconnect`(263–282 行)
  - tunnel 生命週期事件 handler(317–328 行)
  - `clearPinRetry`(248–251 行,順帶清除失敗計數)

**Interfaces:**
- Consumes: Task 8 的 savedIps 函式;Task 4 的 `tunnel_recovered` payload(`udid`/`ip`/`port`);現有 `wifiTunnelDiscover`、`wifiTunnelStop`、`startWifiTunnelRef`。
- Produces: 無新介面(行為變更)。

- [ ] **Step 1: 加 import 與失敗計數 ref**

檔頭 import:

```typescript
import { readSavedIps, removeSavedIpByUdid, upsertSavedIp, writeSavedIps } from '../utils/savedIps'
```

(`wifiTunnelDiscover` 加入現有的 `../services/api` import 清單。)

243 行 `pinRetryTimers` 旁新增:

```typescript
  const pinRetryFailures = useRef<Record<string, number>>({})
```

`clearPinRetry`(248–251 行)內加一行 `delete pinRetryFailures.current[udid]`:

```typescript
  const clearPinRetry = useCallback((udid: string) => {
    const tmr = pinRetryTimers.current[udid]
    if (tmr) { clearTimeout(tmr); delete pinRetryTimers.current[udid] }
    delete pinRetryFailures.current[udid]
  }, [])
```

- [ ] **Step 2: 改寫 `schedulePinReconnect` 的 attempt**

263–282 行整個 `schedulePinReconnect` 替換為:

```typescript
  const schedulePinReconnect = useCallback((udid: string, delayMs = 5000) => {
    if (pinRetryTimers.current[udid]) return // already scheduled
    const attempt = async () => {
      delete pinRetryTimers.current[udid]
      // Stop if the user unpinned, or the tunnel already came back.
      if (!pinnedRef.current.includes(udid)) return
      if (tunnelsRef.current.some((tn) => tn.udid === udid)) return
      const entry = readSavedEntryFor(udid)
      const failures = pinRetryFailures.current[udid] ?? 0
      if (entry && failures < 2) {
        try {
          await startWifiTunnelRef.current?.(entry.ip, entry.port, udid)
          return // success path clears the timer via startWifiTunnel
        } catch {
          pinRetryFailures.current[udid] = failures + 1
        }
      } else {
        // Two direct failures (or nothing saved) — the iPhone likely
        // rebound its RemotePairing port or moved to a new DHCP lease.
        // Re-discover once and try the fresh endpoints; identity is
        // verified after connect (drop anything not pinned).
        try {
          const dres = await wifiTunnelDiscover()
          for (const d of dres?.devices || []) {
            if (tunnelsRef.current.some((tn) => tn.udid === udid)) break
            try {
              const info = await startWifiTunnelRef.current?.(
                String(d.ip), Number(d.port) || 49152, udid,
              )
              if (!info) continue
              if (info.udid === udid) return // reconnected our target
              if (!pinnedRef.current.includes(info.udid)) {
                // Reached an unpinned stranger — undo and scrub.
                await wifiTunnelStop(info.udid).catch(() => {})
                writeSavedIps(removeSavedIpByUdid(readSavedIps(), info.udid))
              }
              // A different pinned device is a keeper; keep looking for ours.
            } catch { /* try next candidate */ }
          }
        } catch { /* discover failed — retry cycle continues below */ }
        pinRetryFailures.current[udid] = 0 // next cycle starts with the saved entry again
      }
      if (pinnedRef.current.includes(udid) && !tunnelsRef.current.some((tn) => tn.udid === udid)) {
        pinRetryTimers.current[udid] = setTimeout(attempt, 15000)
      }
    }
    pinRetryTimers.current[udid] = setTimeout(attempt, delayMs)
  }, [])
```

- [ ] **Step 3: `tunnel_recovered` 更新 savedips**

317–328 行的事件 handler 中,`else if (msg.type === 'tunnel_recovered' || msg.type === 'device_connected')` 分支改為:

```typescript
      } else if (msg.type === 'tunnel_recovered' || msg.type === 'device_connected') {
        const udid = msg.data?.udid
        if (udid) clearPinRetry(udid)
        if (msg.type === 'tunnel_recovered' && udid && msg.data?.ip) {
          // The watchdog may have recovered on a NEW endpoint (port
          // rebind / DHCP change). Persist it so the next launch and
          // future pin retries use the fresh address instead of the
          // stale one that just failed.
          writeSavedIps(upsertSavedIp(readSavedIps(), {
            ip: String(msg.data.ip),
            port: Number(msg.data.port) || 49152,
            udid,
            lastUsed: Date.now(),
          }))
        }
      }
```

- [ ] **Step 4: 型別檢查與測試**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: 無型別錯誤,測試全 PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useDevice.ts
git commit -m "feat: 釘選重試 2 次失敗後改跑偵測,tunnel_recovered 回寫新端點至 savedips"
```

---

### Task 11: 端對端手動驗證

**Files:** 無(驗證任務)。開發環境啟動方式:`cd backend && py -3.13 main.py`(終端 1)+ `cd frontend && npx vite --host --port 5173`(終端 2),瀏覽器開 `http://localhost:5173`。注意 WiFi tunnel 需系統管理員權限終端。日誌:`%USERPROFILE%\.locwarp\logs\backend.log`。

- [ ] **Step 1: 基本迴歸 — 啟動自動連線(埠未變)**

安裝版 LocWarp 若在執行中先關閉。啟動開發版,確認先前連過的釘選 iPhone 在 App 開啟後自動連上(日誌出現 `WiFi tunnel connected`),UI 裝置 chip 變綠。

- [ ] **Step 2: 核心情境 — iPhone 重開機(埠必變)**

1. 讓一支釘選 iPhone 連線中,將它重開機。
2. 預期日誌順序:`Tunnel ... exited unexpectedly` → 3 次 `Tunnel restart attempt`(失敗)→ `Fallback restart attempt for ... via <ip>:<新埠>` → `Tunnel restart succeeded`。
3. 全程不做任何手動操作,UI 裝置 chip 應自動回綠。
4. 確認前端 localStorage `locwarp.tunnel.savedips` 對應 UDID 的條目已更新為新埠(DevTools → Application → Local Storage)。

- [ ] **Step 3: 核心情境 — 重開 App(埠已變)**

1. 承上(savedips 裡是新埠)。再重開機 iPhone 一次讓埠再變,但這次**在 iPhone 重開機期間關閉 LocWarp**。
2. iPhone 開機完、加入 WiFi 後,啟動 LocWarp。
3. 預期:啟動自動連線經由 discover 找到新埠自動連上(儘管 savedips 是舊埠),無需手動偵測。

- [ ] **Step 4: usbmux 退避驗證**

本機未裝 Apple Mobile Device Service:確認 backend.log 只在啟動時出現一次 `usbmuxd unreachable ... USB discovery paused`,之後 60 秒內不再重複,不再有每 3 秒的 `Failed to list usbmux devices` traceback。

- [ ] **Step 5: 殭屍推送觀察**

觸發一次 Step 2 的斷線重連(或等它自然發生),觀察重連後的 `DVT location set ... [udid]` 行:同一 udid 是否只有一條座標流在推進(座標序列單調沿路線前進,而非兩條交錯)。若發現同一 udid 交錯兩條流,記錄日誌片段開 issue/回報 — 修復屬後續範圍(spec 附帶小修 2 的但書)。

- [ ] **Step 6: 完成回報**

整理以上 5 步的結果(成功/失敗/日誌摘要),回報給使用者;全數通過即可考慮開 PR。

---

## Self-Review 紀錄

- **Spec 覆蓋**:設計 §1→Task 2、§2→Task 3+4、§3→Task 8+9、§4→Task 10、§5.1→Task 5、§5.2→Task 6+11(Step 5)、測試→Task 1/7 + 各任務 TDD 步驟、手動驗證→Task 11。無缺口。
- **佔位符掃描**:無 TBD/TODO;Task 4 未附單元測試已明文說明原因與替代把關(Task 3 純函式測試 + Task 11 手動驗證)。
- **型別/簽名一致性**:`find_fallback_endpoints(ip)`(Task 3 定義、Task 4 使用)、`buildAutoConnectCandidates`/`SavedIpEntry`(Task 8 定義、Task 9/10 使用)、`tunnel_recovered` payload `ip`/`port`(Task 4 定義、Task 10 使用)、`UsbmuxAvailability`(Task 5 定義與使用)均一致。
