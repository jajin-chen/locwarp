const { app, BrowserWindow, Menu, shell, ipcMain, dialog } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const http = require('http')
const os = require('os')
const fs = require('fs')

// Render-mode preference (Issue #24). Win 10 stays on software rendering
// by default — v0.2.121/125 hit a Chromium 124 GPU-sandbox crash on
// 22H2 — but users whose hardware works fine can opt in via Settings
// and restart. Win 11 defaults to hardware acceleration as usual.
const RENDER_MODE_FILE = path.join(app.getPath('userData'), 'render-mode.json')

function readRenderModePref() {
  try {
    const raw = fs.readFileSync(RENDER_MODE_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    if (parsed && (parsed.mode === 'hardware' || parsed.mode === 'software')) {
      return parsed.mode
    }
  } catch { /* missing or corrupt — fall through to default */ }
  return null
}

function writeRenderModePref(mode) {
  try {
    fs.mkdirSync(path.dirname(RENDER_MODE_FILE), { recursive: true })
    fs.writeFileSync(RENDER_MODE_FILE, JSON.stringify({ mode }, null, 2), 'utf8')
  } catch (e) {
    console.error('[render-mode] failed to save pref:', e && e.message)
  }
}

if (process.platform === 'win32') {
  const winBuild = parseInt((os.release() || '0.0.0').split('.')[2] || '0', 10)
  const isWin10 = winBuild > 0 && winBuild < 22000
  const saved = readRenderModePref()
  // Effective mode: saved pref wins; otherwise Win 10 → software, Win 11 → hardware.
  const mode = saved || (isWin10 ? 'software' : 'hardware')
  if (mode === 'software') {
    app.disableHardwareAcceleration()
    app.commandLine.appendSwitch('no-sandbox')
    app.commandLine.appendSwitch('in-process-gpu')
  }
}

// Locate-PC over IPC: shells out to PowerShell + System.Device.Location
// (the Windows Location API). This taps Windows' built-in Wi-Fi
// positioning + GPS without needing a Google API key (which Electron's
// navigator.geolocation requires) or any third-party HTTP service.
// Accuracy in urban areas is typically 30-100m; rural ~500m.
const LOCATE_PS_SCRIPT = `
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Device
  $watcher = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High)
  $watcher.Start()
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $deadline) {
    if ($watcher.Permission -eq 'Denied') { Write-Output 'DENIED'; exit 0 }
    if ($watcher.Status -eq 'Ready' -and -not $watcher.Position.Location.IsUnknown) { break }
    Start-Sleep -Milliseconds 200
  }
  if ($watcher.Permission -eq 'Denied') { Write-Output 'DENIED'; exit 0 }
  $loc = $watcher.Position.Location
  if ($loc.IsUnknown) { Write-Output ('NODATA,status=' + $watcher.Status); exit 0 }
  Write-Output ('OK,' + $loc.Latitude + ',' + $loc.Longitude + ',' + $loc.HorizontalAccuracy)
  $watcher.Stop()
} catch {
  Write-Output ('ERROR,' + $_.Exception.Message)
}
`

// Run an HTTPS GET from the Electron main process (no renderer CORS,
// no Content-Security-Policy block) and return the parsed JSON. Used
// by the IP-geolocation fallback chain inside the locate-pc handler.
const httpsGetJson = (url) => {
  return new Promise((resolve) => {
    const https = require('https')
    const req = https.get(url, { headers: { 'User-Agent': 'LocWarp-Electron' }, timeout: 6000 }, (res) => {
      if (res.statusCode !== 200) {
        res.resume()
        return resolve(null)
      }
      let chunks = []
      res.on('data', (c) => chunks.push(c))
      res.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))) }
        catch { resolve(null) }
      })
    })
    req.on('error', () => resolve(null))
    req.on('timeout', () => { try { req.destroy() } catch {} ; resolve(null) })
  })
}

const ipFallback = async () => {
  // ipwho.is — no key, no signup, HTTPS, returns latitude/longitude in JSON.
  const a = await httpsGetJson('https://ipwho.is/')
  if (a && typeof a.latitude === 'number' && typeof a.longitude === 'number') {
    return { ok: true, lat: a.latitude, lng: a.longitude, accuracy: 5000, via: 'ipwho.is' }
  }
  // ipapi.co — backup, also no key.
  const b = await httpsGetJson('https://ipapi.co/json/')
  if (b && b.latitude != null && b.longitude != null) {
    const lat = parseFloat(b.latitude); const lng = parseFloat(b.longitude)
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      return { ok: true, lat, lng, accuracy: 5000, via: 'ipapi.co' }
    }
  }
  // freeipapi.com — last resort.
  const c = await httpsGetJson('https://freeipapi.com/api/json/')
  if (c && c.latitude != null && c.longitude != null) {
    const lat = parseFloat(c.latitude); const lng = parseFloat(c.longitude)
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      return { ok: true, lat, lng, accuracy: 5000, via: 'freeipapi.com' }
    }
  }
  return null
}

const tryWindowsLocation = () => {
  return new Promise((resolve) => {
    let settled = false
    const finish = (payload) => { if (!settled) { settled = true; resolve(payload) } }
    const child = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', LOCATE_PS_SCRIPT],
      { windowsHide: true },
    )
    let out = ''
    child.stdout.on('data', (d) => { out += d.toString('utf8') })
    child.stderr.on('data', (d) => console.error('[locate-pc] stderr:', d.toString('utf8')))
    child.on('error', (e) => finish({ ok: false, code: 'SPAWN_FAILED', message: e.message }))
    child.on('exit', () => {
      const trimmed = out.trim()
      if (trimmed.startsWith('OK,')) {
        const parts = trimmed.split(',')
        const lat = parseFloat(parts[1])
        const lng = parseFloat(parts[2])
        const acc = parseFloat(parts[3])
        if (Number.isFinite(lat) && Number.isFinite(lng)) {
          return finish({ ok: true, lat, lng, accuracy: Number.isFinite(acc) ? acc : 100 })
        }
      }
      if (trimmed === 'DENIED') return finish({ ok: false, code: 'DENIED', message: 'Windows Location service is off or app access denied' })
      if (trimmed.startsWith('NODATA')) return finish({ ok: false, code: 'NODATA', message: trimmed.slice(0, 200) })
      if (trimmed.startsWith('ERROR,')) return finish({ ok: false, code: 'ERROR', message: trimmed.slice(6, 200) })
      finish({ ok: false, code: 'UNKNOWN', message: trimmed.slice(0, 200) || 'no PowerShell output' })
    })
    setTimeout(() => {
      try { child.kill() } catch { /* ignore */ }
      finish({ ok: false, code: 'TIMEOUT', message: 'PowerShell timed out after 18s' })
    }, 18000)
  })
}

ipcMain.handle('get-render-mode', () => {
  // Surface the current saved mode + whether the OS is the one we
  // originally bypassed (Win 10), so the Settings panel can decide
  // whether to highlight this toggle as relevant.
  let isWin10 = false
  if (process.platform === 'win32') {
    const winBuild = parseInt((os.release() || '0.0.0').split('.')[2] || '0', 10)
    isWin10 = winBuild > 0 && winBuild < 22000
  }
  const saved = readRenderModePref()
  // If no pref exists and we're not on Win 10, the effective mode is
  // hardware (current default for Win 11). On Win 10 with no pref, we
  // already prompted at startup, so this branch shouldn't normally hit.
  const effective = saved || (isWin10 ? 'software' : 'hardware')
  return { mode: effective, saved, isWin10 }
})

ipcMain.handle('set-render-mode', (_e, mode) => {
  if (mode !== 'hardware' && mode !== 'software') return { ok: false }
  writeRenderModePref(mode)
  return { ok: true }
})

ipcMain.handle('relaunch-app', () => {
  app.relaunch()
  appQuitting = true
  stopBackend()
  app.exit(0)
})

ipcMain.handle('locate-pc', async () => {
  const win = await tryWindowsLocation()
  if (win.ok) return { ...win, via: 'windows' }
  if (win.code === 'DENIED') return win
  // Windows Location returned NODATA / TIMEOUT / ERROR / UNKNOWN. Fall
  // back to IP geolocation from the main process so the request is
  // free of any renderer CORS / CSP restrictions.
  const ip = await ipFallback()
  if (ip) return ip
  // Both layers failed — surface the original Windows error so the
  // dialog can show the user something diagnostic instead of just
  // "everything failed".
  return {
    ok: false,
    code: 'ALL_FAILED',
    message: `Windows Location: ${win.code}${win.message ? ' (' + win.message + ')' : ''} | IP fallback: all 3 services unreachable`,
  }
})

// Strip the default "File Edit View Window Help" menubar — LocWarp has its
// own in-window controls and the native menu only adds noise on Windows.
Menu.setApplicationMenu(null)

let mainWindow
let backendProc = null
let appQuitting = false
let backendFailureShown = false

function resolveBackendExe() {
  // In a packaged build, extraResources places files under process.resourcesPath
  // (e.g.  .../resources/backend/locwarp-backend.exe).  In dev, we don't spawn;
  // the developer runs `python main.py` manually.
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', 'locwarp-backend.exe')
  }
  return null
}

function resolveBackendInstallDir(exe) {
  const exeDir = path.dirname(exe)
  const exeRoot = path.dirname(path.dirname(exe))
  const resourcesRoot = process.resourcesPath
    ? path.dirname(process.resourcesPath)
    : null
  const candidates = [resourcesRoot, exeRoot, process.resourcesPath, exeDir]
    .filter((dir) => typeof dir === 'string' && dir.length > 0)
  return candidates.find((dir) => fs.existsSync(dir)) || exeRoot || exeDir
}

// The backend exe is an unsigned PyInstaller bundle, which antivirus engines
// (Defender included) sometimes quarantine hours after a successful install.
// Without this, the missing file surfaces as a raw "spawn ... ENOENT" uncaught
// exception that says nothing about what to do. Show an actionable message and
// offer to open the folder so the user can see for themselves it is gone.
function showBackendMissingDialog(exe, detail) {
  if (appQuitting || backendFailureShown) return
  backendFailureShown = true

  const zh = (app.getLocale() || '').toLowerCase().startsWith('zh')
  const installDir = resolveBackendInstallDir(exe)
  const msg = zh
    ? {
        title: 'LocWarp 無法啟動',
        message: '找不到背景服務 (locwarp-backend.exe)',
        detail:
          '這個檔案通常是被防毒軟體 (Windows Defender 或第三方防毒) 判定為可疑並隔離刪除。\n\n' +
          '解決步驟：\n' +
          '1. 開啟「Windows 安全性」→「病毒與威脅防護」→「防護歷程記錄」，若有 LocWarp 相關項目請按「還原」。\n' +
          '2. 在「排除項目」中新增資料夾：\n' +
          `   ${installDir}\n` +
          '3. 移除 LocWarp 後重新安裝 (安裝檔請以系統管理員身分執行)。\n\n' +
          '若使用第三方防毒，請先將 LocWarp 加入白名單再重裝。\n\n' +
          `預期路徑：\n${exe}` +
          (detail ? `\n\n${detail}` : ''),
        buttons: ['開啟安裝資料夾', '關閉'],
      }
    : {
        title: 'LocWarp cannot start',
        message: 'Backend service not found (locwarp-backend.exe)',
        detail:
          'This file is usually removed by antivirus software (Windows Defender or a third-party product) that flagged it as suspicious.\n\n' +
          'How to fix:\n' +
          '1. Open Windows Security > Virus & threat protection > Protection history, and restore any LocWarp entry.\n' +
          '2. Add an exclusion for the folder:\n' +
          `   ${installDir}\n` +
          '3. Uninstall LocWarp, then reinstall (run the installer as administrator).\n\n' +
          'With third-party antivirus, allowlist LocWarp before reinstalling.\n\n' +
          `Expected path:\n${exe}` +
          (detail ? `\n\n${detail}` : ''),
        buttons: ['Open install folder', 'Close'],
      }

  const choice = dialog.showMessageBoxSync({
    type: 'error',
    title: msg.title,
    message: msg.message,
    detail: msg.detail,
    buttons: msg.buttons,
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  })
  if (choice === 0) {
    // Wait for the shell handoff to finish before quitting. openPath resolves
    // with an error string on some platforms and can also reject/throw.
    Promise.resolve()
      .then(() => shell.openPath(installDir))
      .then((error) => {
        if (error) console.error('[electron] failed to open install folder:', error)
      })
      .catch((error) => console.error('[electron] failed to open install folder:', error))
      .finally(() => {
        appQuitting = true
        app.quit()
      })
    return
  }
  appQuitting = true
  app.quit()
}

function startBackend() {
  const exe = resolveBackendExe()
  if (!exe) return
  if (!fs.existsSync(exe)) {
    console.error('[electron] backend exe missing:', exe)
    showBackendMissingDialog(exe, null)
    return
  }
  console.log('[electron] spawning backend:', exe)
  let child
  try {
    child = spawn(exe, [], {
      cwd: path.dirname(exe),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
  } catch (e) {
    console.error('[electron] backend spawn threw:', e)
    showBackendMissingDialog(exe, String(e && e.message ? e.message : e))
    return
  }
  backendProc = child
  // spawn() reports ENOENT/EACCES asynchronously; without this handler the
  // error becomes an uncaught exception in the main process.
  child.once('error', (e) => {
    console.error('[electron] backend spawn error:', e)
    if (backendProc !== child) return
    backendProc = null
    if (appQuitting || backendFailureShown) return
    showBackendMissingDialog(exe, String(e && e.message ? e.message : e))
  })
  child.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`))
  child.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`))
  child.on('exit', (code) => {
    console.log('[electron] backend exited with code', code)
    if (backendProc === child) backendProc = null
  })
}

function stopBackend() {
  const child = backendProc
  backendProc = null
  if (!child) return
  try { child.kill() } catch {}
}

function waitForBackend(timeoutMs = 30000) {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get('http://127.0.0.1:8777/docs', (res) => {
        res.destroy()
        resolve()
      })
      req.on('error', () => {
        if (Date.now() - started > timeoutMs) return reject(new Error('backend timeout'))
        setTimeout(tick, 500)
      })
    }
    tick()
  })
}

async function createWindow() {
  // OSM tile policy (https://operations.osmfoundation.org/policies/tiles/)
  // requires an identifying User-Agent; Electron's default Chrome UA is
  // blocked with HTTP 418. Rewrite the UA on requests to the OSM tile
  // endpoints so we can use the 'Standard' (Mapnik) style for free.
  try {
    const { session } = require('electron')
    const OSM_HOSTS = [
      'tile.openstreetmap.org',
      'a.tile.openstreetmap.org',
      'b.tile.openstreetmap.org',
      'c.tile.openstreetmap.org',
      'tile.openstreetmap.fr',
      'a.tile.openstreetmap.fr',
      'b.tile.openstreetmap.fr',
      'c.tile.openstreetmap.fr',
    ]
    session.defaultSession.webRequest.onBeforeSendHeaders((details, cb) => {
      try {
        const u = new URL(details.url)
        if (OSM_HOSTS.includes(u.hostname)) {
          details.requestHeaders['User-Agent'] =
            `LocWarp/${app.getVersion()} (+https://github.com/keezxc1223/locwarp)`
          details.requestHeaders['Referer'] = 'https://github.com/keezxc1223/locwarp'
        }
      } catch {}
      cb({ requestHeaders: details.requestHeaders })
    })
  } catch (e) { console.error('[electron] UA hook failed:', e) }

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: 'LocWarp',
    // Match the app's dark theme so the initial frame isn't white while
    // the renderer attaches — previously caused a jarring white flash.
    backgroundColor: '#0f1117',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
      // Default Chromium blocks AudioContext output until a user gesture
      // happens on the page; that breaks the route-completion alert
      // sound when a long loop finishes while the user is away from the
      // window. LocWarp is a desktop tool (not a random webpage), so
      // disable the gesture gate entirely.
      autoplayPolicy: 'no-user-gesture-required',
    },
  })
  // Show the window once the first frame is painted. Combined with
  // backgroundColor above, this eliminates the blank/white boot state.
  mainWindow.once('ready-to-show', () => { mainWindow.show() })

  // Open target="_blank" / external links in the user's default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url)
      return { action: 'deny' }
    }
    return { action: 'deny' }
  })

  const isDev = process.argv.includes('--dev') || !app.isPackaged
  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
  } else {
    // Spawn the backend in parallel and load the UI immediately. The
    // renderer already has fetch-with-retry so it rides out the backend
    // startup race — no need to block loadFile on waitForBackend() and
    // stare at a blank window for seconds.
    startBackend()
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

app.whenReady().then(createWindow)
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    appQuitting = true
    app.quit()
  } else {
    stopBackend()
  }
})
app.on('before-quit', () => {
  appQuitting = true
  stopBackend()
})
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow() })
