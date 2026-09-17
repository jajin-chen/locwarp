# iOS Wi-Fi 反覆連線失敗：2026-09-17 根因與驗證

## 結論與目前界線

已獨立重現 Windows WinTun 建立失敗，並改為每台 iPhone 一個隔離的 userspace 隧道程序。這是繞開已證實故障的主機依賴，不代表 Windows 驅動本身已修好。

三台手機已同時通過 API、獨立程序、WebSocket 與實際畫面驗證。最後的恢復修正於 22:43 使用者再次重啟後載入，並在真實 TCP reset 後成功自動恢復定位與隨機漫步。Pauline 的底層 TCP reset 原因仍未確認，不能將自動恢复等同長時間不掉線。

## 五個 Why

1. **為何開啟程式仍連不上？** RemotePairing 成功不等於定位服務可用；隧道在後續的 Windows 虛擬網卡建立階段失敗。
2. **為何隧道建立失敗？** 脫離 LocWarp 的原生 TunTapDevice 測試仍出現 WinTun Code 56、OSError 4319，約等待 15 秒。主機啟動服務後重試仍失敗。更深的 Windows 組態原因尚未證实，不能歸因手機或直接聲稱必須重開機。
3. **為何過去常被看成手機逾時？** 同步 WinTun 呼叫阻塞事件迴圈，超過外層 8 秒候選連線期限；主機錯誤被一般 timeout／候選失敗流程掩蓋。
4. **為何重試與先前修復無法持續解決？** 預設啟動仍依賴故障的 WinTun，舊 userspace 模式只支援同程序單台手機；缺席手機的搜尋還會用錯誤 UDID 探測健康手機的 IP。
5. **為何看似接上又掉？** 首次連線與 watchdog 重連走不同路徑：後者未傳入 userspace 專用 RSD，改用 Windows IPv6 socket 而得到 10051。另有重複接手共享 RSD、關閉期間仍公開 readiness 的競態。僅驗證首次連線，漏掉多台同時使用、掉線恢復與競態。

## 修正

- `LocWarp.bat` 預設 `LOCWARP_TUNNEL_TRANSPORT=userspace-process`。每台手機有自己的 Python 子程序與 userspace stack；loopback relay 使用每次生成的 token，固定目的裝置並限制連線數。
- 保留 `LocWarp.bat kernel` 診斷模式。WinTun 初始化移至背景執行緒，保留 HostTunnelError，避免誤判後掃描大量候選；前端遇到主機錯誤暫停自動重試。
- 所有首次接手及 watchdog 恢復傳入 runner 所有的 RSD。DeviceManager 借用該 RSD，不擅自關閉；重複完整連線保留原 lease、engine、程序。
- runner 開始關閉時立即撤下公開 RSD／info；API 與 watchdog 在取得 lifecycle lock 後重新驗證 runner、endpoint、RSD 及取消狀態。
- 缺席手機自動搜尋前後查詢目前隧道所有權，跳過其他健康手機 IP 的所有埠；WebSocket 恢復事件保留 IP／port，狀態查詢失敗則延後。
- 子程序底層斷線、異常退出與例外記錄到後端日誌；正常要求關閉不冒充異常。保留 Windows SelectorEventLoop。

## 驗證紀錄（Asia/Taipei）

- 獨立兩台實機 userspace 測試完成 RSD 與 DVT connect／clear ACK，未碰當時仍健康的另一台 kernel 連線。
- 使用者於 22:32 重啟後，後端 PID 54924 載入新的多程序傳輸；兩台 Pauline 首次連線與使用者的定位指令成功。
- 22:36 捕捉 Pauline 斷線後 watchdog 使用主機 IPv6 而失敗 10051，據此補修並加入成功恢復測試。
- kate 的 TCP 埠可達但曾 RemotePairing timeout；22:38 獨立配對探測成功，隨後 API 成功接上。使用者表示 kate 不在旁邊，沒有證據認定鎖屏是根因。
- 22:38 三台 API `is_connected=true`，各自程序 PID 6616／58208／35344；三次冪等連線保留相同程序，WebSocket ping/pong 成功。新瀏覽器頁面 A／B／C 都显示已連線。
- 後續仍看到底層 TCP／DVT 斷線。當時主後端尚未載入 watchdog 修正，恢復仍失敗；不能將早先三台快照當作持續健康。
- 最終程式：後端完整測試 **287 passed**（1 個既有 Starlette 棄用警告）；前端 **45 passed**、TypeScript 與 Vite build 通過；獨立 Python reviewer **115 passed**、前端 reviewer 核准。`git diff --check` 通過，僅換行格式提示。

測試會匯入 main 並寫入同一 backend.log，因此診斷時應依真實裝置 UDID／IP 分辨測試事件，不能把 `udid-1` 或 `192.0.2.*` 當成實機故障。

## 最後載入與驗收

工具自動核准檢查曾拒絕停止原有程序（`blocked by policy`），使用者於 22:43 完成第二次手動重啟；後端 PID 35992，已載入最後的重連／競態修正。

- 三台首次建立的獨立程序為 Pauline 12564、Pauline (2) 19556、kate 42632；三次冪等連線保留程序，WebSocket ping/pong 與新畫面三台已連線一致。
- Pauline 於 22:44:35、22:45:20、22:45:55 發生底層 `ConnectionResetError`；每次 watchdog 都成功重建定位服務。22:46:05 日誌明確記錄 `Tunnel restart succeeded`、恢復 random_walk snapshot，隨後有實際 teleport 與漫步更新。
- 35 秒 WebSocket 觀察取得 kate 69 次、Pauline (2) 70 次、Pauline 47 次 position_update，並收到 Pauline 的 tunnel_degraded、tunnel_recovered、device_connected 與 teleport 事件。
- 這段期間另兩台保持原有程序，沒有被 Pauline 恢復流程重建。
- 新日誌揭露子程序自然退出時 daemon buffered stdin 的 Python shutdown fatal error；實際 subprocess 測試先重現 exit 3221225477，再以 raw `os.read` 修正並證明 stdin 仍開啟時正常 exit 0。21 個程序測試及獨立審查通過。此項子程序修正由之後新建的 child 載入，既有 child 不會熱更新；它修正退出延遲，不是先發生的 TCP reset 原因。
- 已確認現有 TCP keepalive 為 3 秒 idle／3 秒 interval；每台僅有一個 owning child 的 control/data socket，沒有證據支持缺少 keepalive 或重複隧道是 reset 根因。
- 22:46:57–22:47:52 以 pktmon 僅擷取 Pauline `.103` TCP 的前 80 bytes，期間沒有 RST，無法回推先前 reset 來源。擷取已停止，臨時篩選器已移除；診斷 ETL／文字檔留在系統 TEMP 的 `locwarp-pauline-reset-20260917.*`。
- kate 於 22:47:56 自然斷線，22:48:06 watchdog 恢復成功，接回 walk_count=2 的漫步，實際定位成功；新程序 40612 載入 raw stdin 修正。Pauline 程序 41212、Pauline (2) 19556 持續存活。
- 22:44:18–22:48:18 每 30 秒狀態觀察：9 個採樣均有三台 connected／三個隧道。中間有上述真實中斷，採樣不是零掉線證明。22:48:24 最新畫面三台均為「模擬中」。

後續應持續區分底層 reset 與恢復成功。不要用普通主機 TCP 探測 userspace RSD IPv6；它只能經由相應子程序的 dial 連線。

本輪未重啟 Windows、未刪除驅動、未變更實體網卡／預設路由，未 commit／push。多程序模式目前適用 Python 原始碼啟動；frozen 執行檔明確拒絕啟用，尚未驗證打包版本。一般直接啟動 backend 不會自動繼承批次檔的 transport 環境變數。

## 參考

- [Microsoft Code 56 定義](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/cm-prob-need-class-config)：類別設定未完成，不能僅靠代碼推論必須重開機。
- [WinTun adapter 原始碼](https://git.zx2c4.com/wintun/tree/api/adapter.c)：對照實際 adapter 建立與狀態錯誤。
