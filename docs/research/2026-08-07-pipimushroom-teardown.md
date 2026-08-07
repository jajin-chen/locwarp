# pipimushroom.com 技術拆解(皮克敏 Bloom 社群地圖)

- 日期:2026-08-07
- 對象:<https://pipimushroom.com/>
- 性質:外部參考研究,非 LocWarp 功能規格
- 方法:只讀取該站公開提供的資源(HTML / CSS / JS / 圖磚 / 回應標頭),未嘗試繞過其防護、未送出任何寫入請求

## 這個站是什麼

**皮克敏 Bloom(Pikmin Bloom,Niantic)的台灣社群回報地圖**。使用者回報並查看蘑菇、
煙霧、花苞、花朵的位置,支援篩選、定位與分享。服務範圍寫死在台灣經緯度框內:

```js
window.pikminTileTaiwanBounds = {south:21.7, west:118.0, north:26.4, east:122.3};
```

站內頁面:`mfmap.aspx`(蘑菇地圖,首頁)、`ppmushroom.aspx`(蘑菇統計)、
`accmap.aspx`(純點)、`mfhelp.aspx`(說明)、`login.aspx`(登入)。

之所以值得研究:它與 LocWarp 面對同一類使用者(Niantic 系定位遊戲玩家),
而且是少數把「防爬蟲」認真做起來的同類站點。

## 技術堆疊

| 層 | 內容 | 證據 |
| --- | --- | --- |
| Web server | IIS 8.5(= Windows Server 2012 R2,已終止支援) | `Server: Microsoft-IIS/8.5` |
| Runtime | .NET Framework 4.x | `X-AspNet-Version: 4.0.30319`、`X-Powered-By: ASP.NET` |
| 框架 | **ASP.NET Web Forms**(非 MVC / 非 Core) | `.aspx` 頁面、`__VIEWSTATE` / `__VIEWSTATEGENERATOR` 隱藏欄位、整頁包在 `<form method="post" id="form1">` |
| API | `.ashx` 泛型處理常式 | `Handlers/MfMapData.ashx`、`Handlers/MapPoiReport.ashx`、`PikminApiSession.ashx` |
| 前端 | **無框架**,手寫 vanilla JS | 無 React / Vue / jQuery;只有 Leaflet 1.x + Font Awesome |
| 地圖 | Leaflet(`Content/vendor/leaflet/`、`Scripts/vendor/leaflet.js`) | |
| 建置 | 每頁一支 minify bundle 於 `Scripts/dist/` | `mfmap.min.js`、`ppmushroom.min.js`、`page-menu.min.js`、`site-disclaimer.min.js`、共用 `api-security.min.js` |
| 快取失效 | **手動版本字串**,非內容雜湊 | `?v=20260801-sea-bubble3`、`?v=20260731-mobile-shortcuts1` |
| 分析 | Google Analytics 4 | `G-1BR3PSKVDX` |
| 主機 | 台灣單一 IP `211.23.87.88`(中華電信 HiNet 網段),Let's Encrypt 憑證,**無 CDN / 無 Cloudflare**,直連來源站 | |

API 端點皆為 POST-only,GET 會拿到結構化 JSON 錯誤:

```
GET /Handlers/MfMapData.ashx  ->  405  {"ok":false,"error":"Method not allowed."}
```

## 值得借鑑的兩個設計

### 1. 自架靜態 OSM 圖磚庫

```js
window.pikminTileUrl              = "/osm1/{z}/{x}/{y}.png";
window.pikminTileMaxZoom          = 19;
window.pikminTileMaxNativeZoom    = 18;
window.pikminTileFallbackUrl      = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
```

`/osm1/` **不是即時代理,是預先下載好、直接放在磁碟上的靜態檔**,由 IIS 靜態檔處理常式吐出:

```
GET /osm1/14/13725/7003.png
  200  image/png  104187 bytes  58ms
  Last-Modified: Fri, 29 Apr 2022 08:05:32 GMT
  ETag: "8e40ebe59f5bd81:0"
```

2022 年一次批次抓下整個台灣範圍的圖磚自存,只在缺圖時回落到 OSM 官方伺服器。
好處是不吃 OSM 的用量政策、不受其限速、延遲低。代價是圖資凍結在下載當時
(該站圖磚已四年未更新),以及需自行承擔儲存空間。

> 對 LocWarp 的意義:LocWarp 目前六個圖層全部直連第三方圖磚服務。若未來遇到
> 圖磚供應商限速或離線使用需求,「侷限地理範圍 + 預先下載 + 回落線上」是一個
> 已被驗證可行的低成本作法。

### 2. 加密簽章的 API 層(`api-security.min.js`)

全站投入最多心力之處,目的明顯是防爬——社群遊戲地圖被抓資料是常態。

**混淆**:以 javascript-obfuscator 處理,字串表輪轉 + 自訂 base64 字母表
(小寫在前:`abcdefghijklmnopqrstuvwxyzABCDEF...`),並啟用 self-defending
(字串表內可見 `debugger` 陷阱、`while (true) {}`、`constructor` 的
function-toString 檢測正規式)。

**機制**(自字串表還原):

1. 全域掛載 `window.PikminApi.request(...)`,各頁面 bundle 一律透過它呼叫 API
2. 先向 `PikminApiSession.ashx` 握手,取得 `encKey`、`sigKey`、`token`、`expiresUntil`
3. 每個請求以 Web Crypto(`crypto.subtle`)做 **AES-CBC 加密 + HMAC 簽章**,
   並帶單調遞增的 `counter` 作重放保護
4. 回應同樣需驗簽(錯誤字串:「API 回應簽章格式錯誤」)
5. 伺服器端有速率限制(「操作太頻繁,請稍後再試。」)
6. 回報有去重與人工確認佇列(「這個地點已有人回報,請等待確認。」)

**評估**:這一層擋得住隨手寫的爬蟲,但金鑰終究要交到瀏覽器手上,擋不住真心要抓
的人。投入產出比需依實際威脅衡量——對這個站而言,提高門檻讓多數人放棄可能已達
目的。

## 整體評價

典型的台灣個人／小團隊長期維護專案:伺服器堆疊老舊但穩定(Web Forms +
Server 2012 R2),前端手寫卻用上了現代 Web Crypto,而防爬蟲的力氣下得比整個 UI
還多。

一個有趣的歷史殘留:地圖頁實質上是純 JS 應用,完全不需要 postback,但整頁仍被
Web Forms 的 `<form>` 與 VIEWSTATE 包著,該 VIEWSTATE 已無實際作用。

## 附錄:重現方式

```bash
# 標頭與原始 HTML
curl -sSL -D - https://pipimushroom.com/ -o page.html

# 前端 bundle
curl -sS -o apisec.js "https://pipimushroom.com/Scripts/dist/api-security.min.js"

# 圖磚(觀察 Last-Modified 判定為靜態檔)
curl -sSI "https://pipimushroom.com/osm1/14/13725/7003.png"
```

還原混淆字串表:找到 `function _0xb4e8()` 的陣列字面值,逐項以自訂字母表
轉回標準 base64 後解碼即可(不需執行該檔任何邏輯)。
