# VirtualRun 跟隨模式(LocWarp ↔ VirtualRun 同步移動)設計

- 日期:2026-08-01
- 狀態:已核准(方案 A)
- 範圍:跨兩個 repo — `D:\locwarp`(本 repo)與 `D:\Android\VirtualRun`(Android App)
- 硬性約束:**絕不能影響 LocWarp 既有 iOS 模擬功能**。follower 純觀察、只讀不寫;任何 follower 端錯誤都不得波及模擬引擎。

## 目標

LocWarp 在 Windows 上模擬 iPhone 移動時(跳點 / 導航 / 多點 / 花農 / 隨機漫步 / 搖桿),
Android 手機上的 VirtualRun 以「跟隨模式」接收同一串座標並套用 Mock Location,
讓 iPhone 與 Android 兩台裝置同步移動。跟隨期間 VirtualRun 照常累計距離、步數、
卡路里,結束時照常寫入 Health Connect。

## 已確認的需求決策

| 決策點 | 結論 |
| --- | --- |
| 傳輸方式 | WiFi 區網 WebSocket(VirtualRun 連 LocWarp) |
| 跟隨行為 | 照常計步 + 寫入 Health Connect |
| 多台 iOS 裝置 | 跟隨 LocWarp 的主要裝置(primary udid) |
| 配對方式 | mDNS 自動發現 + 手動輸入 IP 備援 |
| 方向性 | 單向:LocWarp 為 leader,VirtualRun 為 follower;不回傳任何控制指令 |

## 1. 通訊協定 — `/ws/follow`(LocWarp 端新增)

新檔 `backend/api/follow.py`,獨立 WebSocket 端點,與既有 `/ws/status` 完全分離。
JSON 文字訊息,協定版本欄位 `protocol: 1`。

Server → Client 訊息:

| type | 欄位 | 來源 | 語意 |
| --- | --- | --- | --- |
| `hello` | `app:"locwarp"`, `protocol:1`, `version`, `udid`(主要裝置或 null) | 連線建立時 | 握手,client 檢查 protocol 相容 |
| `position` | `lat`, `lng` | `position_update` 事件 | 正常移動,follower 計里程 |
| `teleport` | `lat`, `lng` | `teleport` 事件 | 跳點,follower 直接跳、不計里程 |
| `sim_state` | `state`(idle / navigating / …) | `state_change` 事件 | 顯示用狀態 |

- 只轉發主要裝置(`primary_udid`)的事件;其他裝置的事件一律丟棄。
- Client → Server 不定義任何指令(單向);收到的文字忽略。
- Keepalive 靠 WebSocket 內建 ping/pong(FastAPI/uvicorn 預設)。

### 掛載方式與隔離

`main.py` 既有的 `event_callback` 內,在 `broadcast(...)` 之後追加:

```python
try:
    await follow.forward(event_type, data)
except Exception:
    logger.debug("follow forward error (ignored)", exc_info=True)
```

- `follow.forward` 內部再自行 try/except 每一條 follower 連線;送失敗直接移除該連線,不 retry、不 block。
- 不修改 `simulation_engine.py`、`location_service.py`、`device_manager.py`、`websocket.py` 的任何既有邏輯。

## 2. mDNS 自動發現(LocWarp 端)

- 啟動時(FastAPI lifespan)用 `zeroconf` 註冊服務 `_locwarp-follow._tcp.local.`:
  - port:8777(`config.API_PORT`)
  - TXT:`protocol=1`, `version=<app version>`, `path=/ws/follow`
- 關閉時註銷。
- 註冊失敗(zeroconf 衝突、防火牆、無網卡)只記 warning,**絕不阻擋啟動、絕不影響其他功能**。
- `zeroconf` 已是 pymobiledevice3 的相依套件;在 `requirements.txt` 明確宣告版本。

## 3. VirtualRun「跟隨」模式(Android 端)

### UI

- 頂部模式切換由「路線 / 自由」擴為「路線 / 自由 / **跟隨**」。
- 跟隨分頁內容:
  - 掃描清單:`NsdManager` 探索 `_locwarp-follow._tcp`,列出找到的電腦(名稱 + IP)。
  - 手動輸入列:`IP[:port]`(port 預設 8777),與掃描並存;記住上次成功連線的位址,下次進入分頁自動帶入。
  - 連線後顯示:連線狀態、LocWarp 模擬狀態(`sim_state`)、即時距離 / 步數 / 卡路里(沿用既有資訊卡)。
  - 行進方式選擇(走路 / 慢跑 / 快跑 / 騎腳踏車)照常顯示:**速度由 LocWarp 座標流決定**,此選擇只決定步幅(換算步數)、卡路里係數、是否計步(騎車不計步)與 Health Connect 運動類型。

### Service 整合

- `SimulationService` 新增 `FOLLOW` 狀態(`SimState.FOLLOW`)與 `ACTION_START_FOLLOW`(帶 host/port)。
- 新增 `follow/` 套件:
  - `LocWarpDiscovery` — NsdManager 探索封裝。
  - `FollowClient` — OkHttp WebSocket client(新增 OkHttp 依賴),解析協定訊息,回呼給 service。
- 座標處理(在既有 mock 迴圈內):
  - `position` → 平滑移動到目標點(沿用藍點平滑機制),兩點間距離累計進里程;步數 = 累計距離 ÷ 目前行進方式步幅(騎車不計);卡路里照既有公式。
  - `teleport` → 直接設位、不計里程(與自由模式「傳送」一致)。
  - 防呆:單次 `position` 跳動 > 100 公尺視同 teleport(不計里程),避免掉訊息後暴衝灌里程。
- 斷線處理:
  - WS 斷線 → 指數退避自動重連(1s、2s、4s … 上限 30s),期間**保持最後位置持續 mock**,通知列顯示「重連中」。
  - LocWarp 端模擬停止(`sim_state: idle`)→ 保持最後位置持續 mock,直到使用者在 VirtualRun 按停止。
- 停止跟隨 → 照常寫入 Health Connect(teleport 段已排除在里程外),還原真實定位,發送結果通知。

## 4. 錯誤處理總表

| 情境 | 行為 |
| --- | --- |
| follower 連線送訊息失敗(LocWarp) | 移除該連線,記 debug log,iOS 管線不受影響 |
| `follow.forward` 拋例外(LocWarp) | `event_callback` 的 try/except 吞掉,只記 log |
| mDNS 註冊失敗(LocWarp) | warning log,功能降級為手動 IP,啟動照常 |
| VirtualRun 掃不到裝置 | 顯示手動輸入 IP 引導 |
| VirtualRun WS 連不上 / 斷線 | 指數退避重連,保持最後位置 mock |
| `hello.protocol` 不相容 | VirtualRun 顯示「請更新 LocWarp / VirtualRun」並斷線 |
| Mock 權限未設 | 沿用既有 `mockReady` 引導流程 |

## 5. 測試

### LocWarp(pytest)

- **隔離回歸測試**:`follow.forward` 拋例外時,`event_callback` 仍正常完成 `broadcast` 與 `update_last_position`(iOS 管線不死)。
- 協定單元測試:事件 → 協定訊息的轉換與過濾(非 primary udid 丟棄、事件型別對映、hello 內容)。
- 死連線清理:送失敗的 follower 被移除,後續 forward 不再嘗試。

### VirtualRun(JUnit)

- 跟隨距離 / 步數累計:連續 position 正確累計;teleport 與 >100m 跳動不計里程。
- 騎車模式不計步。
- 協定解析:hello / position / teleport / sim_state 與未知型別忽略。
- E2E 手動驗證:實機 + LocWarp 實跑(導航、跳點、搖桿、停止模擬、斷線重連)。

## 6. 明確不做(YAGNI)

- VirtualRun → LocWarp 的反向控制(遙控 LocWarp)。
- 跟隨非 primary 的指定裝置(之後有需求再加裝置選擇)。
- 加密 / 認證(區網內明文 WS,與既有 `/ws/status` 同等安全假設)。
- USB adb 傳輸備援。
