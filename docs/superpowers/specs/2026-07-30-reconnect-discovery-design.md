# 偵測輔助重連(Discovery-Assisted Reconnect)設計

日期:2026-07-30
狀態:已核准(brainstorming 完成)

## 背景與問題

使用者回報:已配對過的 iPhone 經常無法自動重連,必須手動執行「無線偵測 → 找到裝置 → 手動連線」才能成功。

### 根本原因(已由程式碼與 `~/.locwarp/logs/backend.log` 證實)

iPhone 端 RemotePairing 服務的埠號是動態的(49152–65535),裝置重開機、暫離 WiFi 後埠號幾乎必變,IP 也可能被 DHCP 換掉。而 LocWarp 全部三條自動重連路徑都只拿「上次成功的 IP:Port」去試,從不重新偵測:

1. **啟動自動連線**(`frontend/src/App.tsx` 約 465–480 行):有釘選裝置時只試 savedips 裡釘選 UDID 的舊 IP:Port,並完全跳過 mDNS 偵測(issue #35 防連錯裝置的副作用)。
2. **後端 watchdog**(`backend/api/device.py` `_per_tunnel_watchdog` / `_TUNNEL_RESTART_BACKOFF`):tunnel 死掉後只用原 IP:Port 重試 3 次(3/6/12 秒退避,共約 21 秒)就 teardown。
3. **前端釘選重試迴圈**(`frontend/src/hooks/useDevice.ts` `schedulePinReconnect`):每 15 秒用 savedips 舊條目重試,同樣不偵測。

### 環境觀察(2026-07-29 深夜日誌 + 2026-07-30 即時重現)

- 特定 iPhone(192.168.1.109)的 tunnel TCP socket 每 30–60 秒死一次(`got OSError in sock-read-task`);埠號未變時 watchdog 能救回,埠號一變即進入上述死路。keepalive 已開啟仍會斷,屬環境性問題(WiFi 省電/漫遊),不在本設計解決範圍,但 watchdog 強化後可自動康復。
- 本機未安裝 Apple Mobile Device Service(usbmuxd),後端每 3 秒 USB 輪詢即報 `ConnectionFailedToUsbmuxdError` 全 traceback,灌爆日誌。

## 使用情境與安全前提

- 使用者環境:最多 3 支 iPhone,全為家人固定裝置,家用區網無外人裝置。
- 仍須保留 issue #35 的意圖:不可自動連上非預期(未釘選)的裝置。把關點從「連線前用舊資料猜」改為「連線後用 handshake 取得的真實 UDID 驗證,不符立即斷開」。

## 設計

### 1. 後端:偵測邏輯抽成可重用函式

把 `/wifi/tunnel/discover` 端點內的 mDNS 瀏覽 + 智慧 /24 子網掃描邏輯抽成獨立 async 函式 `discover_tunnel_candidates() -> list[{ip, port, name}]`,端點與 watchdog 共用。行為不變。

### 2. 後端:watchdog 加偵測後備(核心)

`_per_tunnel_watchdog` 在現行 3 次原埠重試全數失敗後,teardown 前多兩層後備,依序:

1. **同 IP 埠掃描**:呼叫現成 `_scan_ports_for_ip(ip)`(掃 49152–65535,數秒完成),對找到的每個埠試 `_attempt_tunnel_restart`。涵蓋最常見的「IP 沒變、埠變了」(iPhone 重開機/暫離 WiFi)。
2. **完整偵測**:埠掃描無果才跑 `discover_tunnel_candidates()`,對每個候選端點試 restart。涵蓋「DHCP 換 IP」。

限制與規則:

- 每層各跑一次,全部失敗即走現有 teardown 流程;不無限循環。
- **UDID 驗證**:restart 成功後檢查 handshake 取得的 identifier 是否等於 watchdog 原本追蹤的 UDID;不符視同失敗 — 關閉該 tunnel、續試下一個候選。
- `tunnel_recovered` 廣播 payload 增加實際使用的 `ip` 與 `port`(target 端點,非 RSD 位址),供前端更新 savedips。

### 3. 前端:啟動自動連線改為「偵測 + 事後驗證」

`App.tsx` 啟動自動連線:有釘選時不再跳過 mDNS 偵測。

- 候選 = 釘選 UDID 的 savedips 條目 + `wifiTunnelDiscover()` 結果(去重、上限 3)。
- 連上後檢查 `res.udid`:有釘選且不在釘選清單 → 立即呼叫 stop tunnel 斷開,且不寫入 savedips。
- 無釘選時維持現行為。

### 4. 前端:釘選重試迴圈加偵測

`schedulePinReconnect` 每 15 秒重試:先試 savedips 條目;**連續 2 次失敗後**,該次改跑一次 discover,對結果套用與第 3 節相同的「UDID 驗證、連錯即斷」。避免每 15 秒都掃網路。

- 前端收到 `tunnel_recovered`(含新 ip/port)時更新 savedips 對應 UDID 條目,下次啟動即用新端點。

### 5. 附帶小修

1. **usbmux 退避**:USB 輪詢遇 `ConnectionFailedToUsbmuxdError` 時退避 60 秒再試;只在「可用 ↔ 不可用」狀態轉換時記一行 WARNING,不再每 3 秒印全 traceback。
2. **殭屍推送驗證**:`DVT location set` 日誌加上 udid 前綴,以便區分多裝置推送。實作時驗證 watchdog 重啟後舊引擎確實停止;若確認有殭屍任務持續推送舊座標,修正 park/cancel 邏輯(範圍僅限確認到的洩漏點)。

## 錯誤處理

- 偵測後備各層單次、依序、有界;任何一層丟例外都記 log 並落入下一層/teardown,不得讓 watchdog 協程死掉。
- UDID 不符一律「斷開 + 試下一候選」,不得保留錯誤裝置的 tunnel。
- 前端 discover 失敗靜默(現行為),savedips 條目仍會嘗試。

## 測試

- **後端單元測試**(mock `TunnelRunner`、`_scan_ports_for_ip`、`discover_tunnel_candidates`):
  - 原埠重試失敗 → 埠掃描找到新埠 → restart 成功。
  - 埠掃描無果 → discover 找到新 IP → restart 成功。
  - UDID 不符 → 斷開並續試下一候選;全部不符 → teardown。
  - 全部失敗 → teardown(現行為不變)。
- **前端測試**:啟動自動連線候選合併(釘選 savedips + discover 去重、上限 3);連上非釘選 UDID → 呼叫 stop tunnel;`tunnel_recovered` 更新 savedips。
- **手動驗證**:iPhone 重開機後(埠號必變)不做任何手動操作,LocWarp 應自動回連。

## 不做的事(YAGNI)

- 不改 tunnel 傳輸協定(TCP → QUIC)或試圖解決環境性 socket 斷線本身。
- 不做常駐週期性全網掃描。
- 不動多裝置群組同步、DDI 掛載等相鄰子系統。
