import React, { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useT } from './i18n'
import { useWebSocket } from './hooks/useWebSocket'
import { useDevice } from './hooks/useDevice'
import { useSimulation } from './hooks/useSimulation'
import { useJoystick } from './hooks/useJoystick'
import { useBookmarks } from './hooks/useBookmarks'
import { useGeoContext } from './hooks/useGeoContext'
import { useSavedRoutes } from './hooks/useSavedRoutes'
import UserAvatarPicker from './components/UserAvatarPicker'
import { useAvatar } from './hooks/useAvatar'
import * as api from './services/api'
import { parseCoord } from './utils/coords'
import { useWaypointEditing, clampLat, normalizeLng } from './hooks/useWaypointEditing'
import { buildAutoConnectCandidates, pinnedUdidsNeedingRetry } from './utils/autoConnect'
import { readSavedIps, removeSavedIpByUdid, writeSavedIps } from './utils/savedIps'

import MapView from './components/MapView'
import ControlPanel from './components/ControlPanel'
import DeviceStatus from './components/DeviceStatus'
import SettingsPage from './components/SettingsPage'
import JoystickPad from './components/JoystickPad'
import EtaBar from './components/EtaBar'
import PauseControl from './components/PauseControl'
import FlowerSettingsPanel from './components/FlowerSettingsPanel'
import StatusBar from './components/StatusBar'
import { DeviceChipRow } from './components/DeviceChipRow'
import type { FanoutOutcome } from './hooks/useSimulation'

// Summarise a group fan-out result into a single toast string.
// Call from action handlers: showToast(toastForFanout(t, 'teleport', outcome, connectedDevices))
export function toastForFanout<T>(
  t: (k: any, v?: Record<string, string | number>) => string,
  action: string,
  outcome: FanoutOutcome<T>,
  devices: { udid: string }[],
): string {
  const total = outcome.ok.length + outcome.failed.length
  if (total === 0) return action
  if (outcome.failed.length === 0) return t('group.action_all_success', { action })
  if (outcome.ok.length === 0) return t('group.action_all_failed', { action })
  const statusFor = (udid: string) =>
    outcome.ok.some((o) => o.udid === udid) ? 'OK'
      : outcome.failed.find((f) => f.udid === udid)?.reason ?? 'error'
  const letters = ['A', 'B', 'C']
  const parts = devices.slice(0, 3).map((d, i) => `${letters[i]} ${statusFor(d.udid)}`)
  return `${action}: ${parts.join(', ')}`
}

import { SimMode, MoveMode } from './hooks/useSimulation'

const SPEED_MAP: Record<MoveMode, number> = {
  walking: 10.8,
  running: 19.8,
  driving: 60,
}

const App: React.FC = () => {
  const t = useT()
  const ws = useWebSocket()
  const device = useDevice(ws.subscribe)
  // Pass primary-device udid into useSimulation so its legacy single-device
  // setters only react to the primary's WS events in dual-device mode,
  // stopping the map marker from ping-ponging between both devices'
  // independently-jittered positions.
  const sim = useSimulation(ws.subscribe, device.primaryDevice?.udid)
  const joystick = useJoystick(ws.sendMessage, sim.mode === SimMode.Joystick)
  const bm = useBookmarks()

  // Stable bookmark-pin array for the map. Re-mapping inline in JSX builds a
  // fresh array every render, which churns MapView's clustering effect (it
  // rebuilds the whole supercluster index on each new reference). Memoize so
  // the index only rebuilds when the bookmarks themselves change.
  const bookmarkPins = useMemo(
    () => bm.bookmarks.map((b: any) => ({
      id: b.id, name: b.name, lat: b.lat, lng: b.lng, country_code: b.country_code || '',
    })),
    [bm.bookmarks],
  )

  // Bumped every time an external trigger (currently the map topleft
  // library button) wants ControlPanel to open its library panel.
  // ControlPanel reacts on change via useEffect, so we don't have to
  // lift the whole libraryOpen/libraryTab state here.
  const [openLibraryToken, setOpenLibraryToken] = useState(0)
  // First-level navigation rail (iOS-style). Only one page's content shows
  // at a time so the sidebar isn't an endless scroll. 'library' is an
  // action (opens the floating window) rather than a swapped page.
  const [activePage, setActivePage] = useState<'nav' | 'connection' | 'settings'>('nav')
  const [cooldown, setCooldown] = useState(0)
  const [cooldownEnabled, setCooldownEnabled] = useState(false)
  const [randomWalkRadius, setRandomWalkRadius] = useState(500)
  const [clickToAddWaypoint, setClickToAddWaypointRaw] = useState<boolean>(() => {
    try { return localStorage.getItem('locwarp.click_to_add_waypoint') === '1' } catch { return false }
  })
  const setClickToAddWaypoint = useCallback((v: boolean) => {
    setClickToAddWaypointRaw(v)
    try { localStorage.setItem('locwarp.click_to_add_waypoint', v ? '1' : '0') } catch { /* ignore */ }
  }, [])
  const [goldDittoA, setGoldDittoARaw] = useState<string>(() => {
    try { return localStorage.getItem('locwarp.goldditto.a') ?? '' } catch { return '' }
  })
  const setGoldDittoA = useCallback((v: string) => {
    setGoldDittoARaw(v)
    try { localStorage.setItem('locwarp.goldditto.a', v) } catch { /* ignore */ }
  }, [])
  const [goldDittoBusy, setGoldDittoBusy] = useState(false)
  const [showBookmarkPins, setShowBookmarkPinsRaw] = useState<boolean>(() => {
    try { return localStorage.getItem('locwarp.show_bookmark_pins') === '1' } catch { return false }
  })
  const setShowBookmarkPins = useCallback((v: boolean) => {
    setShowBookmarkPinsRaw(v)
    try { localStorage.setItem('locwarp.show_bookmark_pins', v ? '1' : '0') } catch { /* ignore */ }
  }, [])
  const [toastMsg, setToastMsg] = useState<string | null>(null)
  // Active avatar selection + persistent custom-PNG slot + picker state.
  const avatar = useAvatar()
  // Reverse-geo-derived status-bar context (flag / city / timezone /
  // weather), debounced + gated inside the hook so it only fires on
  // discrete user-initiated moves. See useGeoContext for the full rules.
  const locMeta = useGeoContext(sim.currentPosition, sim.status?.state)

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const showToast = useCallback((msg: string, ms = 3000) => {
    // Cancel any previous auto-clear timer so the newest toast always
    // gets its full duration. Otherwise an earlier toast (e.g. teleport,
    // 2s) would fire its clear timer mid-way through a later toast
    // (e.g. timezone, 6s) and blank it out after only a fraction.
    if (toastTimerRef.current !== null) {
      clearTimeout(toastTimerRef.current)
      toastTimerRef.current = null
    }
    setToastMsg(msg)
    toastTimerRef.current = setTimeout(() => {
      setToastMsg(null)
      toastTimerRef.current = null
    }, ms)
  }, [])

  // Saved-route library (routes + categories CRUD, import/export). The
  // load-confirm dialog flow stays below because it fans out teleports.
  const {
    savedRoutes,
    routeCategories,
    handleRouteSave,
    handleRoutesBulkDelete,
    handleRouteMove,
    handleRouteCategoryAdd,
    handleRouteCategoryDelete,
    handleRouteCategoryRename,
    handleRouteCategoryRecolor,
    handleRouteCategoryReorder,
    handleRouteReorder,
    handleGpxImport,
    handleGpxExport,
    handleRoutesImportAll,
    handleRouteRename,
    handleRouteDelete,
  } = useSavedRoutes({ sim, showToast, t })

  const handleRestore = useCallback(async () => {
    // The backend stop + DVT clear can take a few seconds, especially if
    // movement was active or the channel is flaky. Give the user a visible
    // "working on it" toast up front so the UI doesn't feel frozen.
    showToast(t('status.restore_in_progress'), 10000)
    const startedAt = Date.now()
    try {
      // Group mode: fan out restore to every connected device; fall back to
      // the legacy single-engine restore when no devices are tracked yet.
      const udids = device.connectedDevices.map((d) => d.udid)
      if (udids.length >= 2) {
        const outcome = await sim.restoreAll(udids)
        if (outcome.failed.length > 0 && outcome.ok.length === 0) {
          throw new Error(outcome.failed[0]?.reason ?? 'restore failed')
        }
      } else {
        await sim.restore()
      }
      // Keep the in-progress toast visible for at least 1.2 s — otherwise a
      // fast restore (sub-second) would overwrite it before the user even
      // noticed it appeared.
      const elapsed = Date.now() - startedAt
      if (elapsed < 1200) {
        await new Promise((r) => setTimeout(r, 1200 - elapsed))
      }
      showToast(t('status.restore_success_wait'))
    } catch {
      showToast(t('status.restore_failed'))
    }
  }, [showToast, t, sim, device])

  const handleToggleCooldown = useCallback((enabled: boolean) => {
    setCooldownEnabled(enabled)
    api.setCooldownEnabled(enabled).catch(() => setCooldownEnabled((v) => !v))
  }, [])

  // Surface a failed initial bookmark load (useBookmarks only logged it,
  // leaving the sidebar list silently empty).
  useEffect(() => {
    if (!bm.loadError) return
    showToast(bm.loadError, 5000)
    bm.clearLoadError()
  }, [bm.loadError, bm, showToast])

  // Backend broadcasts `route_error` (payload: reason) when a running route
  // aborts mid-way. Toast the reason so the user learns WHY it stopped
  // instead of the sim just going idle.
  useEffect(() => {
    if (!sim.routeError) return
    showToast(sim.routeError, 6000)
    sim.clearRouteError()
  }, [sim.routeError, sim, showToast])


  // Auto-scan devices when WebSocket (re)connects (e.g. after backend restart)
  useEffect(() => {
    if (ws.connected) {
      device.scan()
    }
  }, [ws.connected])

  // Auto-attempt WiFi tunnel on first WS connect if the user previously
  // saved at least one IP/port AND has the auto-connect toggle on. Runs
  // once per app session — not on every WS reconnect — to avoid re-
  // triggering after a backend restart that already restored the tunnel
  // via the backend's own watchdog. Failures are silent (the WiFi panel
  // will surface them when the user opens it).
  //
  // Multi-device: tries every IP/port pair in `locwarp.tunnel.savedips`
  // in parallel (up to MAX_TUNNEL_DEVICES = 3) so a user with two or
  // three iPhones gets all of them connecting at once, not just the
  // most recent one. Falls back to the legacy single-IP keys for users
  // upgrading from a build that didn't track multiple IPs yet.
  //
  // Group-mode safety: each per-IP attempt is independent. The whole
  // pass is skipped if a device is already connected at trigger time
  // (USB plug, or backend already brought a tunnel back up via its own
  // restart logic) so we don't fight with an existing USB connection.
  const wifiAutoConnectAttemptedRef = useRef(false)
  useEffect(() => {
    if (!ws.connected) return
    if (wifiAutoConnectAttemptedRef.current) return
    let enabled: boolean
    let savedList: Array<{ ip: string; port: number; udid?: string }> = []
    try {
      enabled = localStorage.getItem('locwarp.tunnel.autoconnect') !== '0'
      const raw = localStorage.getItem('locwarp.tunnel.savedips') || '[]'
      try {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          savedList = parsed
            .filter((e) => e && typeof e.ip === 'string' && e.ip.trim())
            .map((e) => ({
              ip: String(e.ip).trim(),
              port: Number(e.port) || 49152,
              udid: typeof e.udid === 'string' && e.udid ? e.udid : undefined,
            }))
        }
      } catch { /* ignore — fall back to legacy below */ }
      // Legacy single-IP fallback for upgraders.
      if (savedList.length === 0) {
        const legacyIp = (localStorage.getItem('locwarp.tunnel.ip') || '').trim()
        if (legacyIp) {
          const portStr = localStorage.getItem('locwarp.tunnel.port') || '49152'
          savedList = [{ ip: legacyIp, port: parseInt(portStr, 10) || 49152 }]
        }
      }
    } catch {
      return
    }
    if (!enabled) return
    wifiAutoConnectAttemptedRef.current = true
    // Defer so device.scan() and any backend-side restored tunnels have
    // time to surface in `device.connectedDevices` before we decide
    // whether auto-connect is needed.
    const tid = setTimeout(() => {
      ;(async () => {
        try {
          // Skip if a device is already connected (USB plug, or backend
          // already brought a tunnel back up via its own restart logic).
          if (device.connectedDevices.length > 0) return
          const status = await api.wifiTunnelStatus()
          const alreadyTunneled = new Set(
            (status?.tunnels || [])
              .map((tn) => tn.ip && tn.port
                ? `${tn.ip}:${tn.port}`
                : `${tn.rsd_address || ''}:${tn.rsd_port || 0}`),
          )
          // Two sources for auto-connect candidates:
          //   1. savedips: previously-connected iPhones (UDID known)
          //   2. mDNS / subnet discover: iPhones currently broadcasting
          //      their RemotePairing service (UDID unknown until handshake)
          // Discover catches the case where a user connected a second
          // iPhone via the auto-connect path itself (so it never went
          // through the manual save) — without it, only one iPhone keeps
          // auto-connecting on every launch even though both are paired.
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
          // Case-insensitive identity check: pair-record filenames / RSD
          // peer_info can differ in case from the udid we originally saved
          // (backend already compares UDIDs case-insensitively — see
          // backend/api/device.py). Compare lowercased so a legitimately
          // pinned phone isn't mistaken for an unpinned stranger and kicked.
          const pinnedUdidsLc = pinnedUdids.map((u) => u.toLowerCase())

          let discovered: Array<{ ip: string; port: number; ports?: number[] }> = []
          try {
            const dres = await api.wifiTunnelDiscover()
            discovered = (dres?.devices || []).map((d: any) => ({
              ip: String(d.ip),
              port: Number(d.port) || 49152,
              ...(Array.isArray(d.ports)
                ? { ports: d.ports.map(Number).filter((p: number) => Number.isInteger(p) && p > 0 && p <= 65535) }
                : {}),
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
              const info = await device.startWifiTunnel(
                entry.ip, entry.port, entry.udid, entry.ports,
              ).catch(() => null)
              if (!info) return
              if (pinnedUdidsLc.length > 0 && !pinnedUdidsLc.includes(info.udid.toLowerCase())) {
                // Discovery reached a device the user never pinned —
                // undo the connect and scrub it from savedips so it
                // doesn't come back next launch.
                await device.stopTunnel(info.udid).catch(() => {})
                writeSavedIps(removeSavedIpByUdid(readSavedIps(), info.udid))
              }
            }),
          )
          // Cold-start retry: the pin retry loop (schedulePinReconnect)
          // otherwise only arms off the `tunnel_lost` WS event, which only
          // fires for a device that connected THIS session and later
          // dropped. If a pinned iPhone is already offline at launch
          // (locked screen, mid-reboot, not back on WiFi yet), the single
          // attempt above just fails and nothing ever retries it — the
          // user is stuck manually clicking Discover -> Connect. Re-check
          // which tunnels are actually up now and arm the retry loop for
          // every pinned UDID still missing.
          if (pinnedUdids.length > 0) {
            const finalStatus = await api.wifiTunnelStatus().catch(() => null)
            const liveTunnelUdids = (finalStatus?.tunnels || []).map((tn) => String(tn.udid || ''))
            const needsRetry = pinnedUdidsNeedingRetry({ pinnedUdids, liveTunnelUdids })
            needsRetry.forEach((udid, index) => {
              // Stagger start times so several offline pinned phones don't
              // all trigger a full mDNS + /24 + full-range discovery scan
              // at the same moment — that fallback path (see
              // schedulePinReconnect in useDevice.ts) is heavy enough that
              // running it concurrently for 3 devices can swamp the LAN.
              device.schedulePinReconnect(udid, 5000 + index * 5000)
            })
          }
        } catch {
          // Silent — tunnel section will show its own error when opened.
        }
      })()
    }, 1500)
    return () => clearTimeout(tid)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.connected])

  // Poll cooldown
  useEffect(() => {
    if (!ws.connected) return
    const id = setInterval(() => {
      api.getCooldownStatus().then((s: any) => {
        setCooldown(s.remaining_seconds ?? 0)
        if (typeof s.enabled === 'boolean') setCooldownEnabled(s.enabled)
      }).catch(() => {})
    }, 2000)
    return () => clearInterval(id)
  }, [ws.connected])

  // Recent places list (last 20 destinations the user flew to). Loaded
  // once on mount; refreshed after each push so the map's recent-button
  // popover is always current.
  const [recentPlaces, setRecentPlaces] = useState<api.RecentEntry[]>([])
  const refreshRecent = useCallback(async () => {
    try { setRecentPlaces(await api.getRecent()) } catch { /* silent */ }
  }, [])
  useEffect(() => { void refreshRecent() }, [refreshRecent])
  const pushRecent = useCallback(async (lat: number, lng: number, kind: api.RecentKind, name?: string) => {
    try {
      await api.pushRecent({ lat, lng, kind, name: name || null })
      void refreshRecent()
      // When the caller didn't supply a name (right-click teleport /
      // navigate, coord-input fly), reverse-geocode in the background
      // and push again with a resolved short_name. Backend dedupe then
      // bumps the top entry and fills in its name field, so the list
      // stops showing the raw coord twice.
      if (!name) {
        void (async () => {
          try {
            const geo = await api.reverseGeocode(lat, lng)
            const resolved = String(geo?.short_name || geo?.display_name || '').trim()
            if (!resolved) return
            await api.pushRecent({ lat, lng, kind, name: resolved })
            void refreshRecent()
          } catch { /* offline / rate-limited — keep the unnamed entry */ }
        })()
      }
    } catch { /* silent */ }
  }, [refreshRecent])
  const clearRecentList = useCallback(async () => {
    try { await api.clearRecent() } catch { /* silent */ }
    setRecentPlaces([])
  }, [])

  // Waypoint-list editing (map-click add, insert-after mode, move / remove /
  // trim, random generation, fly-confirm dialog, route bulk-paste).
  const {
    wpGenRadius,
    setWpGenRadius,
    wpGenCount,
    setWpGenCount,
    handleGenerateRandomWaypoints,
    handleGenerateAllRandom,
    insertAfterIndex,
    handleInsertAfterWp,
    cancelInsertMode,
    handleMapClick,
    handleAddWaypoint,
    handleClearWaypoints,
    handleRemoveWaypoint,
    handleMoveWaypoint,
    handleSetWpAsStart,
    wpFlyConfirm,
    setWpFlyConfirm,
    confirmWpFly,
    wpCollapsed,
    setWpCollapsed,
    routePasteOpen,
    setRoutePasteOpen,
    routePasteText,
    setRoutePasteText,
    parseRoutePaste,
    submitRoutePaste,
  } = useWaypointEditing({ sim, device, t, showToast, pushRecent, clickToAddWaypoint })

  // `source` lets the caller tag this flight for the recent-places
  // history: 'menu' (map right-click) is the default, 'coord' when the
  // coord-input overlay button fired us. The UI shows different labels
  // depending on source.
  // Preview pin state. Lives at App level so both the coord-input
  // overlay (inside MapView) and the bookmark-list (inside ControlPanel)
  // can drop / clear the same pin. Cleared automatically by any real
  // teleport so the amber "you're peeking" pin doesn't linger after the
  // GPS catches up to the same coordinate.
  const [previewPin, setPreviewPin] = useState<{ lat: number; lng: number } | null>(null)
  const clearPreviewPin = useCallback(() => setPreviewPin(null), [])

  const handleTeleport = useCallback(async (latIn: number, lngIn: number, source: 'menu' | 'coord' = 'menu') => {
    const lat = clampLat(latIn)
    const lng = normalizeLng(lngIn)
    setPreviewPin(null)
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      sim.setCurrentPosition({ lat, lng })
      const outcome = await sim.teleportAll(udids, lat, lng)
      showToast(toastForFanout(t, t('mode.teleport'), outcome, device.connectedDevices))
    } else {
      sim.teleport(lat, lng)
    }
    void pushRecent(lat, lng, source === 'coord' ? 'coord_teleport' : 'teleport')
  }, [sim, device, t, showToast, pushRecent])

  const mapApiRef = useRef<{
    panTo: (lat: number, lng: number, zoom?: number) => void
    fitBounds: (points: { lat: number; lng: number }[]) => void
  } | null>(null)
  const handleMapPanOnly = useCallback((lat: number, lng: number) => {
    const cl = clampLat(lat)
    const nl = normalizeLng(lng)
    mapApiRef.current?.panTo(cl, nl)
    setPreviewPin({ lat: cl, lng: nl })
  }, [])

  const handleNavigate = useCallback(async (latIn: number, lngIn: number, source: 'menu' | 'coord' = 'menu') => {
    const lat = clampLat(latIn)
    const lng = normalizeLng(lngIn)
    setPreviewPin(null)
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      const outcome = await sim.navigateAll(udids, lat, lng)
      showToast(toastForFanout(t, t('mode.navigate'), outcome, device.connectedDevices))
    } else {
      sim.navigate(lat, lng)
    }
    void pushRecent(lat, lng, source === 'coord' ? 'coord_navigate' : 'navigate')
  }, [sim, device, t, showToast, pushRecent])

  const [addBmDialog, setAddBmDialog] = useState<{
    lat: number; lng: number; name: string; category: string;
    countryCode?: string; nameResolving?: boolean;
  } | null>(null)

  const handleAddBookmark = useCallback((lat: number, lng: number) => {
    setAddBmDialog({
      lat,
      lng,
      name: '',
      category: bm.categories[0]?.name || t('bm.default'),
      nameResolving: true,
    })
    // Reverse-geocode asynchronously to pre-fill the name + remember country.
    // User can still overwrite the suggestion. If the call fails we just leave
    // the field blank as before.
    ;(async () => {
      try {
        const geo = await api.reverseGeocode(lat, lng)
        if (!geo) {
          setAddBmDialog((prev) => prev ? { ...prev, nameResolving: false } : prev)
          return
        }
        const cc = String(geo.country_code ?? '').toLowerCase()
        // Backend now returns a clean `short_name` picked from POI / road /
        // area tags (ignoring noisy house-number leading segments like "6").
        // Fall back to first display_name segment only if short_name absent.
        const short = String(geo.short_name || '').trim()
          || String(geo.display_name || '').split(',')[0]?.trim()
          || ''
        setAddBmDialog((prev) => {
          if (!prev) return prev
          // Don't overwrite anything the user already typed.
          if (prev.name && prev.name.length > 0) {
            return { ...prev, countryCode: cc, nameResolving: false }
          }
          return { ...prev, name: short, countryCode: cc, nameResolving: false }
        })
      } catch {
        setAddBmDialog((prev) => prev ? { ...prev, nameResolving: false } : prev)
      }
    })()
  }, [bm.categories, t])

  const submitAddBookmark = useCallback(() => {
    if (!addBmDialog || !addBmDialog.name.trim()) return
    const cat = bm.categories.find(c => c.name === addBmDialog.category)
    bm.createBookmark({
      name: addBmDialog.name.trim(),
      lat: addBmDialog.lat,
      lng: addBmDialog.lng,
      category_id: cat?.id || 'default',
      country_code: addBmDialog.countryCode || '',
    } as any)
    setAddBmDialog(null)
  }, [addBmDialog, bm])

  // Bulk-paste bookmark dialog state. Per-line parser scrapes the first
  // valid lat/lng out of each line via parseCoord — extra label text on
  // the same line ("OK", "#3", "一般火", "(...)" brackets, etc.) is
  // dropped, lines without a coord pair count as invalid.
  const [bulkPasteOpen, setBulkPasteOpen] = useState(false)
  const [bulkPasteText, setBulkPasteText] = useState('')
  const [bulkPasteCategory, setBulkPasteCategory] = useState<string>(() => bm.categories[0]?.name || '預設')
  const [bulkPasteBusy, setBulkPasteBusy] = useState(false)
  const parseBulkPaste = useCallback((raw: string): { valid: Array<{ lat: number; lng: number }>; invalidCount: number; totalLines: number } => {
    const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0)
    const valid: Array<{ lat: number; lng: number }> = []
    let invalidCount = 0
    for (const line of lines) {
      const c = parseCoord(line)
      if (!c) { invalidCount++; continue }
      valid.push({ lat: c.lat, lng: c.lng })
    }
    return { valid, invalidCount, totalLines: lines.length }
  }, [])
  const submitBulkPaste = useCallback(async () => {
    const { valid } = parseBulkPaste(bulkPasteText)
    if (valid.length === 0) {
      showToast(t('bm.bulk_paste_empty'))
      return
    }
    setBulkPasteBusy(true)
    const cat = bm.categories.find((c) => c.name === bulkPasteCategory)
    const catId = cat?.id || 'default'
    let added = 0
    for (const entry of valid) {
      try {
        await bm.createBookmark({
          name: `${entry.lat.toFixed(5)}, ${entry.lng.toFixed(5)}`,
          lat: entry.lat,
          lng: entry.lng,
          category_id: catId,
          country_code: '',
        } as any)
        added++
      } catch { /* skip bad rows */ }
    }
    setBulkPasteBusy(false)
    setBulkPasteOpen(false)
    setBulkPasteText('')
    showToast(t('bm.bulk_paste_done').replace('{count}', String(added)))
  }, [bulkPasteText, bulkPasteCategory, bm, parseBulkPaste, t, showToast])

  const handleGoldDittoStart = useCallback(async () => {
    const raw = goldDittoA.trim()
    if (!raw) {
      showToast(t('goldditto.toast.no_a'))
      return
    }
    const parts = raw.split(',').map((s) => s.trim())
    if (parts.length !== 2) {
      showToast(t('goldditto.toast.invalid_a'))
      return
    }
    const lat = parseFloat(parts[0])
    const lng = parseFloat(parts[1])
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || lat < -90 || lat > 90 || lng < -180 || lng > 180) {
      showToast(t('goldditto.toast.invalid_a'))
      return
    }
    const udids = device.connectedDevices.map((d) => d.udid)
    setGoldDittoBusy(true)
    try {
      if (udids.length >= 2) {
        const outcome = await sim.goldDittoCycleAll(udids, lat, lng)
        showToast(toastForFanout(t, t('mode.goldditto'), outcome, device.connectedDevices))
      } else {
        await sim.goldDittoCycle(lat, lng)
        showToast(t('goldditto.toast.restored'))
      }
    } catch {
      // sim.goldDittoCycle / fan-out helper already surfaces error via setError
    } finally {
      setGoldDittoBusy(false)
    }
  }, [goldDittoA, sim, device, t, showToast])

  const handleStartWaypointRoute = useCallback(async () => {
    // UI waypoint list already includes the current position as index 0
    // (see handleAddWaypoint / generateWaypoints), so just hand it straight
    // to the backend. No more prepend-on-start, no more accidental re-inject
    // on repeated clicks.
    const route = sim.waypoints
    if (route.length < 2) {
      showToast(t('toast.no_waypoints'))
      return
    }
    const udids = device.connectedDevices.map((d) => d.udid)
    // 圈數: 0 = single pass (original 多點導航 → multiStop backend),
    // null = infinite loop, N>0 = N laps (both via startLoop backend).
    if (sim.loopLapCount === 0) {
      if (udids.length >= 2) {
        const outcome = await sim.multiStopAll(udids, route, 0, false)
        showToast(toastForFanout(t, t('mode.loop'), outcome, device.connectedDevices))
      } else {
        sim.multiStop(route, 0, false)
      }
    } else {
      if (udids.length >= 2) {
        const outcome = await sim.startLoopAll(udids, route)
        showToast(toastForFanout(t, t('mode.loop'), outcome, device.connectedDevices))
      } else {
        sim.startLoop(route)
      }
    }
  }, [sim, device, showToast, t])

  // 種花模式 start: circle every placed waypoint. Independent from the
  // 多點路徑 (Loop) start path — it has its own backend handler / settings.
  const handleStartFlower = useCallback(async () => {
    const route = sim.waypoints
    if (route.length < 1) {
      showToast(t('toast.no_waypoints'))
      return
    }
    // Walking to the first flower needs a current position; teleport mode
    // doesn't (it jumps straight onto each flower).
    if (!sim.flowerTeleport && !sim.currentPosition) {
      showToast(t('toast.no_position_random'))
      return
    }
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      const outcome = await sim.flowerAll(udids, route)
      showToast(toastForFanout(t, t('mode.flower'), outcome, device.connectedDevices))
    } else {
      sim.flower(route)
    }
  }, [sim, device, showToast, t])

  // -- ControlPanel handlers --
  const handleStart = useCallback(async () => {
    const udids = device.connectedDevices.map((d) => d.udid)
    if (sim.mode === SimMode.Joystick) {
      if (!sim.currentPosition) {
        // Joystick moves relative to the current sim location; backend rejects
        // start without one. Guide the user instead of surfacing the raw error.
        showToast(t('toast.joystick_need_position'))
        return
      }
      if (udids.length >= 2) {
        const outcome = await sim.joystickStartAll(udids)
        showToast(toastForFanout(t, t('mode.joystick'), outcome, device.connectedDevices))
      } else {
        sim.joystickStart()
      }
    } else if (sim.mode === SimMode.RandomWalk) {
      if (!sim.currentPosition) {
        showToast(t('toast.no_position_random'))
        return
      }
      if (udids.length >= 2) {
        const outcome = await sim.randomWalkAll(udids, sim.currentPosition, randomWalkRadius)
        showToast(toastForFanout(t, t('mode.random_walk'), outcome, device.connectedDevices))
      } else {
        sim.randomWalk(sim.currentPosition, randomWalkRadius)
      }
    } else if (sim.mode === SimMode.Loop) {
      handleStartWaypointRoute()
    } else if (sim.mode === SimMode.Flower) {
      handleStartFlower()
    } else if (sim.mode === SimMode.GoldDitto) {
      handleGoldDittoStart()
    }
  }, [sim, device, randomWalkRadius, handleStartWaypointRoute, handleStartFlower, handleGoldDittoStart, showToast, t])

  const handleStop = useCallback(async () => {
    // Stop the active movement only — keep the simulated location in place
    // so the device stays where the user paused it. Use the 一鍵還原 button
    // separately to clear the simulated location and restore real GPS.
    const udids = device.connectedDevices.map((d) => d.udid)
    if (sim.mode === SimMode.Joystick && udids.length >= 2) {
      const outcome = await sim.joystickStopAll(udids)
      showToast(toastForFanout(t, t('mode.joystick'), outcome, device.connectedDevices))
      return
    }
    if (udids.length >= 2) {
      const outcome = await sim.stopAll(udids)
      showToast(toastForFanout(t, 'stop', outcome, device.connectedDevices))
    } else {
      sim.stop()
    }
  }, [sim, device, t, showToast])

  const [routeLoadConfirm, setRouteLoadConfirm] = useState<{ name: string; waypoints: { lat: number; lng: number }[] } | null>(null)
  // Name of the route currently loaded into the waypoint list, shown in the
  // panel so the user knows which route is active. Cleared automatically
  // once the waypoint list is emptied.
  const [loadedRouteName, setLoadedRouteName] = useState<string | null>(null)
  useEffect(() => {
    if (sim.waypoints.length === 0 && loadedRouteName !== null) setLoadedRouteName(null)
  }, [sim.waypoints.length, loadedRouteName])
  const handleRouteLoad = useCallback((id: string) => {
    const route = savedRoutes.find((r) => r.id === id)
    if (!route || !Array.isArray(route.waypoints) || route.waypoints.length === 0) return
    const wps = route.waypoints.map((w: any) => ({ lat: w.lat, lng: w.lng }))
    setRouteLoadConfirm({ name: route.name ?? '', waypoints: wps })
  }, [savedRoutes])

  const confirmRouteLoad = useCallback(async (flyToStart: boolean) => {
    if (!routeLoadConfirm) return
    const { waypoints } = routeLoadConfirm
    // Loading a route always means the user wants to run it, so switch into
    // the route (路線) mode and fill the waypoint list atomically. Otherwise
    // the app stays in 瞬間移動 (the launch default) and pressing 開始 does
    // nothing because that mode has no waypoint-route handler.
    sim.loadRoute(waypoints)
    // Remember which route is loaded so the panel can show its name.
    setLoadedRouteName(routeLoadConfirm.name || null)
    if (flyToStart && waypoints.length > 0) {
      const first = waypoints[0]
      const udids = device.connectedDevices.map((d) => d.udid)
      // Match wpFly flow: set current position + teleport directly so the
      // device GPS lands on the start point without leaving 路線 mode.
      sim.setCurrentPosition({ lat: first.lat, lng: first.lng })
      if (udids.length > 0) {
        try { await sim.teleportAll(udids, first.lat, first.lng) } catch { /* ignore */ }
      }
      void pushRecent(first.lat, first.lng, 'coord_teleport')
    } else if (!flyToStart && waypoints.length > 0) {
      // "Show waypoints only": don't move the iPhone GPS, but move the
      // MAP view to the route so the user can see where it is instead of
      // having to scroll around looking for it.
      mapApiRef.current?.fitBounds(waypoints)
    }
    setRouteLoadConfirm(null)
  }, [routeLoadConfirm, sim, device, pushRecent])

  const handleApplySpeed = useCallback(async () => {
    const udids = device.connectedDevices.map((d) => d.udid)
    try {
      if (udids.length >= 2) {
        const outcome = await sim.applySpeedAll(udids)
        showToast(toastForFanout(t, t('panel.apply_speed_success'), outcome, device.connectedDevices))
      } else {
        await sim.applySpeed()
        showToast(t('panel.apply_speed_success'))
      }
    } catch (err: any) {
      showToast(t('panel.apply_speed_failed') + (err?.message ? `: ${err.message}` : ''))
    }
  }, [sim, device, showToast, t])

  const handlePause = useCallback(async () => {
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      const outcome = await sim.pauseAll(udids)
      showToast(toastForFanout(t, 'pause', outcome, device.connectedDevices))
    } else {
      sim.pause()
    }
  }, [sim, device, t, showToast])

  const handleResume = useCallback(async () => {
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      const outcome = await sim.resumeAll(udids)
      showToast(toastForFanout(t, 'resume', outcome, device.connectedDevices))
    } else {
      sim.resume()
    }
  }, [sim, device, t, showToast])

  const handleOpenLog = useCallback(async () => {
    try {
      // Open the folder, not the file — log can be large and copy/paste
      // from a multi-MB Notepad window is painful. Folder lets the user
      // attach the file directly to the Issue.
      await api.openLogFolder()
    } catch (err: any) {
      showToast(t('status.open_log_failed') + (err?.message ? `: ${err.message}` : ''))
    }
  }, [showToast, t])

  const handleBookmarkImport = useCallback(async (file: File) => {
    try {
      const isGpx = file.name.toLowerCase().endsWith('.gpx')
      const res = isGpx
        ? await api.importBookmarksGpx(file)
        : await api.importBookmarks(JSON.parse(await file.text()))
      await bm.refresh()
      showToast(t('bm.import_success', { n: res.imported }))
    } catch (err: any) {
      showToast(t('bm.import_failed', { error: err?.message || 'unknown' }))
    }
  }, [bm, showToast, t])

  // ── Hoisted props for memoized children ────────────────────────────
  // Inline object / array / arrow props are re-created on every App render
  // (up to ~10 Hz during a run) and would defeat the React.memo wrappers
  // on ControlPanel / DeviceStatus / StatusBar. Hoist them into
  // useCallback / useMemo so the memo actually holds.
  const handleSpeedChange = useCallback((s: number) => {
    if (s <= 10.8) sim.setMoveMode(MoveMode.Walking)
    else if (s <= 19.8) sim.setMoveMode(MoveMode.Running)
    else sim.setMoveMode(MoveMode.Driving)
  }, [sim])

  const handleAddressSelect = useCallback(async (lat: number, lng: number, name: string) => {
    const latN = clampLat(lat)
    const lngN = normalizeLng(lng)
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length >= 2) {
      sim.setCurrentPosition({ lat: latN, lng: lngN })
      const outcome = await sim.teleportAll(udids, latN, lngN)
      showToast(toastForFanout(t, t('mode.teleport'), outcome, device.connectedDevices))
    } else {
      sim.teleport(latN, lngN)
    }
    void pushRecent(latN, lngN, 'search', name)
  }, [sim, device, t, showToast, pushRecent])

  const panelBookmarks = useMemo(() => bm.bookmarks.map((b: any) => ({
    id: b.id,
    name: b.name,
    lat: b.lat,
    lng: b.lng,
    category: bm.categories.find(c => c.id === b.category_id)?.name || t('bm.default'),
    country_code: b.country_code || '',
    created_at: b.created_at || '',
    last_used_at: b.last_used_at || '',
  })), [bm.bookmarks, bm.categories, t])
  const panelBookmarkCategories = useMemo(() => bm.categories.map(c => c.name), [bm.categories])
  const panelBookmarkCategoryColors = useMemo(
    () => Object.fromEntries(bm.categories.map(c => [c.name, c.color || ''])),
    [bm.categories],
  )

  const handleBookmarkClick = useCallback((b: any) => handleTeleport(b.lat, b.lng), [handleTeleport])
  const handleBookmarkPreview = useCallback((b: any) => handleMapPanOnly(b.lat, b.lng), [handleMapPanOnly])
  const handleBookmarkAdd = useCallback((b: any) => {
    const cat = bm.categories.find(c => c.name === b.category)
    // Reverse-geocode first so custom-coordinate bookmarks also get a
    // country flag. If lookup fails or takes too long, save without
    // one so the user isn't blocked.
    ;(async () => {
      let cc = ''
      try {
        const geo = await Promise.race([
          api.reverseGeocode(b.lat, b.lng),
          new Promise<null>((resolve) => setTimeout(() => resolve(null), 4000)),
        ])
        if (geo && (geo as any).country_code) {
          cc = String((geo as any).country_code).toLowerCase()
        }
      } catch { /* ignore */ }
      bm.createBookmark({
        name: b.name,
        lat: b.lat,
        lng: b.lng,
        category_id: cat?.id || 'default',
        country_code: cc,
      } as any)
    })()
  }, [bm])
  const handleBookmarkDelete = useCallback((id: string) => { void bm.deleteBookmark(id) }, [bm])
  const handleBookmarkEdit = useCallback((id: string, data: any) => {
    // BookmarkList emits UI-shape patches ({name}, or {name,lat,lng,category}).
    // Backend PUT /api/bookmarks requires the full Bookmark schema with
    // category_id (not category name), so merge the patch onto the
    // original and translate category name -> id before sending.
    //
    // If orig is missing (bm.bookmarks briefly out of sync with a
    // background refresh), fall back to the patch data — the edit
    // dialog supplies a full bookmark via spread so we still have the
    // fields we need. This prevents the silent-noop save the user saw
    // after running Fix Flags.
    const orig = bm.bookmarks.find(b => b.id === id)
    const base: any = orig ? { ...orig } : { ...data, id }
    const patch: any = base
    if (data.name != null) patch.name = data.name
    if (data.lat != null) patch.lat = data.lat
    if (data.lng != null) patch.lng = data.lng
    if (data.category != null) {
      const cat = bm.categories.find(c => c.name === data.category)
      if (cat) patch.category_id = cat.id
    }
    // Flag-backfill-on-save: trigger reverse-geocode whenever we'd
    // benefit from a fresh country_code — i.e. coords moved (stale),
    // OR the bookmark never had a flag to begin with (legacy entry
    // from before the feature shipped). Runs in the background so
    // the save itself feels instant.
    const refLat = orig ? orig.lat : base.lat
    const refLng = orig ? orig.lng : base.lng
    const coordsChanged =
      (data.lat != null && data.lat !== refLat) ||
      (data.lng != null && data.lng !== refLng)
    const flagMissing = !base.country_code
    const needsGeocode = coordsChanged || flagMissing
    if (coordsChanged) {
      // Coordinates moved — clear the stale flag so UI doesn't show
      // the wrong country while the async lookup is in flight.
      patch.country_code = ''
    }
    if (needsGeocode) {
      ;(async () => {
        try {
          const geo = await Promise.race([
            api.reverseGeocode(patch.lat, patch.lng),
            new Promise<null>((resolve) => setTimeout(() => resolve(null), 4000)),
          ])
          const cc = geo && (geo as any).country_code
            ? String((geo as any).country_code).toLowerCase()
            : ''
          if (cc) {
            await bm.updateBookmark(id, { ...patch, country_code: cc } as any)
          }
        } catch { /* ignore */ }
      })()
    }
    bm.updateBookmark(id, patch)
  }, [bm])
  const handleCategoryAdd = useCallback((name: string) => {
    // Pick a random preset color at creation so different categories
    // start visually distinct; the color is persisted and stays put
    // across rename (was previously hashed from name → jumped on rename).
    const palette = ['#ef4444', '#f97316', '#eab308', '#22c55e', '#14b8a6', '#3b82f6', '#6366f1', '#a855f7', '#ec4899', '#64748b']
    const color = palette[Math.floor(Math.random() * palette.length)]
    bm.createCategory({ name, color })
  }, [bm])
  const handleCategoryDelete = useCallback((name: string) => {
    const cat = bm.categories.find(c => c.name === name)
    if (cat) bm.deleteCategory(cat.id)
  }, [bm])
  const handleCategoryRename = useCallback((oldName: string, newName: string) => {
    const cat = bm.categories.find(c => c.name === oldName)
    if (!cat) return
    // Default category is immutable (UI also hides the rename button
    // for it, but guard here too in case a stale UI ref slips past).
    if (cat.id === 'default') return
    // Backend PUT requires the full BookmarkCategory shape, keep color.
    bm.updateCategory(cat.id, { ...cat, name: newName })
  }, [bm])
  const handleCategoryRecolor = useCallback((name: string, color: string) => {
    const cat = bm.categories.find(c => c.name === name)
    if (!cat) return
    bm.updateCategory(cat.id, { ...cat, color })
  }, [bm])
  const handleCategoryReorder = useCallback((orderedNames: string[]) => {
    // BookmarkList speaks names; translate to backend ids before
    // POSTing the new order. Skip any name we can't resolve (e.g.
    // the synthetic "Uncategorized" bucket isn't a real category).
    const ids = orderedNames
      .map((n) => bm.categories.find((c) => c.name === n)?.id)
      .filter((id): id is string => !!id)
    if (ids.length > 0) bm.reorderCategories(ids)
  }, [bm])
  const handleBookmarkReorder = useCallback((categoryName: string, orderedIds: string[]) => {
    const cat = bm.categories.find((c) => c.name === categoryName)
    if (!cat) return
    bm.reorderBookmarksInCategory(cat.id, orderedIds)
  }, [bm])
  const handleCategoryExportGpx = useCallback((name: string) => {
    const cat = bm.categories.find((c) => c.name === name)
    if (!cat) return
    window.open(api.bookmarkCategoryGpxExportUrl(cat.id), '_blank')
  }, [bm])
  const handleCategoriesExportGpxZip = useCallback((names: string[]) => {
    const ids = names
      .map((n) => bm.categories.find((c) => c.name === n)?.id)
      .filter((x): x is string => !!x)
    if (ids.length === 0) return
    window.open(api.bookmarkCategoriesGpxZipUrl(ids), '_blank')
  }, [bm])
  const handleBookmarkBulkPasteOpen = useCallback(() => {
    setBulkPasteText('')
    setBulkPasteCategory(bm.categories[0]?.name || '預設')
    setBulkPasteOpen(true)
  }, [bm])

  const panelSavedRoutes = useMemo(() => savedRoutes.map(r => ({
    id: r.id,
    name: r.name,
    waypoints: r.waypoints ?? [],
    profile: r.profile,
    category_id: r.category_id || 'default',
    created_at: r.created_at,
    updated_at: r.updated_at,
  })), [savedRoutes])

  const deviceStatusDevice = useMemo(() => device.connectedDevice ? {
    id: device.connectedDevice.udid,
    name: device.connectedDevice.name,
    iosVersion: device.connectedDevice.ios_version,
    connectionType: device.connectedDevice.connection_type,
    developerModeEnabled: device.connectedDevice.developer_mode_enabled,
  } : null, [device.connectedDevice])
  const deviceStatusDevices = useMemo(() => device.devices.map(d => ({
    id: d.udid,
    name: d.name,
    iosVersion: d.ios_version,
    connectionType: d.connection_type,
    developerModeEnabled: d.developer_mode_enabled,
  })), [device.devices])
  const handleDeviceScan = useCallback(() => { device.scan() }, [device])
  const handleDeviceSelect = useCallback((id: string) => { device.connect(id) }, [device])

  const handleOpenSettings = useCallback(() => setActivePage('settings'), [])

  // Build props for components. sim.currentPosition / sim.destination are
  // already {lat,lng} state objects — pass them through directly instead of
  // re-wrapping (which minted a fresh object every render).
  const currentPos = sim.currentPosition
  const destPos = sim.destination

  // 種花模式 map preview: one segmented polygon per waypoint, matching the
  // backend's circle geometry (radius + segment count) so the user sees
  // exactly what will be walked. Recomputed when the points / settings change.
  const flowerPreview = useMemo(() => {
    if (sim.mode !== SimMode.Flower) return []
    const R = sim.flowerRadius
    const N = Math.max(3, Math.round(sim.flowerSegments))
    return sim.waypoints.map((wp: { lat: number; lng: number }) => {
      const coslat = Math.max(Math.cos((wp.lat * Math.PI) / 180), 1e-6)
      const pts: { lat: number; lng: number }[] = []
      for (let k = 0; k < N; k++) {
        const ang = (2 * Math.PI * k) / N
        pts.push({
          lat: wp.lat + (R * Math.cos(ang)) / 111320,
          lng: wp.lng + (R * Math.sin(ang)) / (111320 * coslat),
        })
      }
      return pts
    })
  }, [sim.mode, sim.waypoints, sim.flowerRadius, sim.flowerSegments])

  // Hoisted + memoized: this large waypoint-editor block used to be built
  // inline in ControlPanel's props on every App render, handing it a fresh
  // ReactNode each time and defeating its React.memo wrapper.
  const modeExtraSection = useMemo(() => (sim.mode === SimMode.Loop || sim.mode === SimMode.Flower) ? (
          <div className="section" style={{ margin: '0 0 8px 0' }}>
            <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="3" />
                <line x1="12" y1="5" x2="12" y2="1" />
                <line x1="12" y1="23" x2="12" y2="19" />
              </svg>
              {sim.mode === SimMode.Flower ? t('mode.flower') : t('panel.waypoints')} ({sim.waypoints.length})
              <span style={{ fontSize: 10, opacity: 0.5, marginLeft: 4 }}>{t('panel.waypoints_hint')}</span>
            </div>
            <div className="section-content">
              {sim.mode === SimMode.Loop && (
                <PauseControl
                  labelKey="pause.loop"
                  value={sim.pauseLoop}
                  onChange={sim.setPauseLoop}
                />
              )}
              {sim.mode === SimMode.Flower && (
                <FlowerSettingsPanel sim={sim} />
              )}
              {sim.mode === SimMode.Loop && (() => {
                const lap = sim.loopLapCount // null = 無限, 0 = 單程(原多點), N = N 圈
                return (
                <div style={{
                  marginBottom: 6, fontSize: 11,
                  display: 'flex', alignItems: 'center', gap: 8,
                }}>
                  <span style={{ opacity: 0.7, whiteSpace: 'nowrap' }}>{t('loop.lap_count_label')}</span>
                  <input
                    type="number"
                    className="lw-input"
                    min={0}
                    placeholder={t('loop.lap_count_placeholder')}
                    value={lap ?? ''}
                    onChange={(e) => {
                      const raw = e.target.value.trim()
                      if (raw === '') { sim.setLoopLapCount(null); return }
                      const n = parseInt(raw, 10)
                      sim.setLoopLapCount(Number.isFinite(n) && n >= 0 ? n : 0)
                    }}
                    style={{ width: 64 }}
                    title={t('loop.lap_count_tooltip')}
                  />
                  <span style={{ opacity: 0.5, fontSize: 10 }}>
                    {lap == null ? t('loop.lap_hint_infinite') : lap === 0 ? t('loop.lap_hint_single') : t('loop.lap_hint_n', { n: lap })}
                  </span>
                  {sim.lapProgress && (
                    <span style={{ opacity: 0.6, fontSize: 10, marginLeft: 'auto' }}>
                      {t('loop.lap_progress', {
                        current: sim.lapProgress.current,
                        total: sim.lapProgress.total ?? '∞',
                      })}
                    </span>
                  )}
                </div>
                )
              })()}
              <div style={{ marginBottom: 6, fontSize: 11 }}>
                {/* Random-waypoint generator (半徑 / 數量 / 隨機產生 / 全隨機) is
                    for 多點路徑 only; 種花模式 places flowers by click / paste. */}
                {sim.mode === SimMode.Loop && (
                <>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4 }}>
                  <span style={{ opacity: 0.7, width: 36 }}>{t('panel.waypoints_radius')}</span>
                  <input
                    type="number"
                    className="lw-input"
                    min={10}
                    value={wpGenRadius}
                    onChange={(e) => setWpGenRadius(Math.max(1, parseInt(e.target.value) || 0))}
                    style={{ flex: 1 }}
                  />
                  <span style={{ opacity: 0.5, width: 16 }}>m</span>
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                  <span style={{ opacity: 0.7, width: 36 }}>{t('panel.waypoints_count')}</span>
                  <input
                    type="number"
                    className="lw-input"
                    min={1}
                    max={50}
                    value={wpGenCount}
                    onChange={(e) => setWpGenCount(Math.max(1, parseInt(e.target.value) || 0))}
                    style={{ flex: 1 }}
                  />
                  <span style={{ opacity: 0.5, width: 16 }}>{t('panel.points')}</span>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button
                    className="action-btn"
                    style={{ flex: 1, padding: '3px 8px', fontSize: 11 }}
                    onClick={handleGenerateRandomWaypoints}
                    title={t('panel.waypoints_gen_tooltip')}
                  >{t('panel.waypoints_generate')}</button>
                  <button
                    className="action-btn"
                    style={{ flex: 1, padding: '3px 8px', fontSize: 11 }}
                    onClick={handleGenerateAllRandom}
                    title={t('panel.waypoints_gen_all_tooltip')}
                  >{t('panel.waypoints_generate_all')}</button>
                </div>
                </>
                )}
                {/* Bulk paste button — Variant D from the mockup: gradient pill
                    with an animated shimmer that hints "this is the eye-catcher". */}
                <button
                  className="route-paste-shimmer"
                  onClick={() => { setRoutePasteText(''); setRoutePasteOpen(true); }}
                  title={t('panel.route_paste_tooltip')}
                  style={{ width: '100%', marginTop: 8 }}
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="9" y="2" width="6" height="4" rx="1"/>
                    <path d="M9 4H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-3"/>
                  </svg>
                  {t('panel.route_paste_button')}
                </button>
              </div>
              {sim.waypoints.length === 0 && (
                <div style={{ fontSize: 12, opacity: 0.5, padding: '4px 0' }}>
                  {t('panel.waypoints_empty')}
                </div>
              )}
              {/* Collapse toggle: a long route would otherwise push the speed /
                  action controls far below. Default collapsed, showing only the
                  current target + next point; expand to edit the full list. */}
              {sim.waypoints.length > 2 && (
                <button
                  className="action-btn"
                  onClick={() => setWpCollapsed((c) => !c)}
                  title={t('panel.waypoints_toggle_tooltip')}
                  style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 8px', fontSize: 11, marginBottom: 4 }}
                >
                  <span style={{ opacity: 0.8 }}>
                    {wpCollapsed ? t('panel.waypoints_show_all') : t('panel.waypoints_collapse')}
                  </span>
                  <span style={{ opacity: 0.6 }}>{wpCollapsed ? '▾' : '▴'}</span>
                </button>
              )}
              {(() => {
                const total = sim.waypoints.length
                const seg = sim.waypointProgress?.current
                // When collapsed, show just the current target (seg+1) and the
                // point after it; before a run starts, fall back to start + next.
                const collapsed = wpCollapsed && total > 2
                let indices: number[]
                if (!collapsed) {
                  indices = sim.waypoints.map((_: any, i: number) => i)
                } else {
                  const base = seg != null ? Math.min(seg + 1, total - 1) : 0
                  indices = Array.from(new Set([base, base + 1].filter((i) => i >= 0 && i < total)))
                }
                return indices.map((i) => {
                  const wp: any = sim.waypoints[i]
                // UI waypoints[0] = the implicit start position (current
                // device location at add-time). Backend seg_idx N = traveling
                // from waypoints[N] toward waypoints[N+1]; the *target* of
                // that segment is waypoints[N+1], so highlight i == seg+1.
                const approaching = seg != null && i === seg + 1
                const passed = seg != null && i <= seg
                const isStart = i === 0;
                return (
                  <div
                    key={i}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px', fontSize: 12,
                      borderRadius: 4, marginBottom: 2,
                      background: approaching ? 'rgba(255, 152, 0, 0.18)' : 'transparent',
                      border: approaching ? '1px solid rgba(255, 152, 0, 0.6)' : '1px solid transparent',
                      opacity: passed ? 0.4 : 1,
                      transition: 'background 0.25s, border-color 0.25s',
                      animation: approaching ? 'wp-pulse 1.4s ease-in-out infinite' : undefined,
                    }}
                  >
                    <span style={{ color: approaching ? '#ff9800' : passed ? '#666' : isStart ? '#4caf50' : '#ff9800', fontWeight: 600, width: 24, fontSize: isStart ? 10 : undefined }}>
                      {approaching ? '>' : passed ? 'OK' : isStart ? t('panel.waypoint_start') : `#${i}`}
                    </span>
                    <button
                      onClick={() => setWpFlyConfirm({ lat: wp.lat, lng: wp.lng, index: i })}
                      title={t('panel.waypoints_click_to_fly')}
                      style={{
                        flex: 1, background: 'transparent', border: 'none',
                        color: 'inherit', opacity: 0.85, textAlign: 'left',
                        padding: 0, cursor: 'pointer',
                        font: 'inherit', letterSpacing: 0,
                      }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.textDecoration = 'underline'; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.textDecoration = 'none'; }}
                    >{wp.lat.toFixed(5)}, {wp.lng.toFixed(5)}</button>
                    {!isStart && (
                      <>
                        <button
                          className="action-btn"
                          style={{ padding: '2px 5px', fontSize: 10, opacity: i <= 1 ? 0.3 : 1 }}
                          onClick={() => handleMoveWaypoint(i, -1)}
                          disabled={i <= 1 || sim.status?.running}
                          title={t('panel.waypoints_move_up')}
                        >↑</button>
                        <button
                          className="action-btn"
                          style={{ padding: '2px 5px', fontSize: 10, opacity: i >= sim.waypoints.length - 1 ? 0.3 : 1 }}
                          onClick={() => handleMoveWaypoint(i, 1)}
                          disabled={i >= sim.waypoints.length - 1 || sim.status?.running}
                          title={t('panel.waypoints_move_down')}
                        >↓</button>
                      </>
                    )}
                    <button
                      className="action-btn"
                      style={{ padding: '2px 6px', fontSize: 10 }}
                      onClick={() => handleRemoveWaypoint(i)}
                      title={t('panel.waypoints_remove')}
                    >X</button>
                  </div>
                );
                })
              })()}
              {sim.waypoints.length > 0 && (
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button
                    className="action-btn"
                    style={{ flex: 1 }}
                    onClick={handleClearWaypoints}
                    disabled={sim.status?.running}
                  >{t('generic.clear')}</button>
                  <button
                    className="action-btn"
                    style={{ flex: 1 }}
                    onClick={async () => {
                      const wps = sim.waypoints
                      if (wps.length === 0) return
                      const txt = wps
                        .map((w: any) => `${w.lat.toFixed(6)}, ${w.lng.toFixed(6)}`)
                        .join('\n')
                      try {
                        await navigator.clipboard.writeText(txt)
                      } catch {
                        const ta = document.createElement('textarea')
                        ta.value = txt
                        document.body.appendChild(ta)
                        ta.select()
                        try { document.execCommand('copy') } catch { /* ignore */ }
                        document.body.removeChild(ta)
                      }
                      showToast(t('toast.waypoints_copied').replace('{n}', String(wps.length)))
                    }}
                    title={t('panel.waypoints_copy_all_tooltip')}
                  >{t('panel.waypoints_copy_all')}</button>
                  {sim.waypoints.length >= 3 && (
                    <button
                      className="action-btn"
                      style={{ flex: 1 }}
                      onClick={async () => {
                        try {
                          const res = await api.routeOptimize(
                            sim.waypoints.map((w: any) => ({ lat: w.lat, lng: w.lng })),
                            sim.moveMode, true, sim.routeEngine, sim.straightLine,
                          )
                          if (res?.waypoints?.length) {
                            sim.setWaypoints(res.waypoints)
                            const baseMsg = t('toast.route_optimized')
                            // When the duration matrix fell back to
                            // haversine (all road-aware engines down),
                            // tag the toast so the user knows the order
                            // is from a straight-line estimate.
                            showToast(res.used_estimate
                              ? `${baseMsg} (${t('toast.route_optimize_estimate')})`
                              : baseMsg)
                          }
                        } catch (err: any) {
                          showToast(err?.message || t('toast.route_optimize_failed'))
                        }
                      }}
                      disabled={sim.status?.running}
                      title={t('panel.waypoints_optimize_tooltip')}
                    >{t('panel.waypoints_optimize')}</button>
                  )}
                </div>
              )}
            </div>
          </div>
  ) : null, [
    sim, wpGenRadius, wpGenCount, wpCollapsed,
    handleGenerateRandomWaypoints, handleGenerateAllRandom, handleMoveWaypoint,
    handleRemoveWaypoint, handleClearWaypoints, showToast, t,
  ])

  // Mode default km/h, used only for ControlPanel's in-panel preset
  // preview and as a very last fallback in the status bar before any
  // apply / sim start has happened.
  const speed = SPEED_MAP[sim.moveMode] || 10.8
  const fmtSpeedFromInputs = (kmh: number | null, lo: number | null, hi: number | null): number | string => {
    if (lo != null && hi != null) return `${Math.min(lo, hi)}~${Math.max(lo, hi)}`
    if (kmh != null) return kmh
    return SPEED_MAP[sim.moveMode] || 10.8
  }

  // Determine running/paused state from status
  const isRunning = sim.status.running
  const isPaused = sim.status.paused

  // Status-bar speed display:
  //  - Idle: reflect the speed the user has *selected* (preset mode default
  //    or custom km/h / range) so picking a speed updates the bar at once.
  //  - Running: show what's actually applied on the device (effectiveSpeed);
  //    changing the selector mid-route still needs 套用新速度 to take effect.
  const selectedSpeedDisplay = fmtSpeedFromInputs(sim.customSpeedKmh, sim.speedMinKmh, sim.speedMaxKmh)
  const displaySpeed: number | string = isRunning && sim.effectiveSpeed
    ? fmtSpeedFromInputs(sim.effectiveSpeed.kmh, sim.effectiveSpeed.min, sim.effectiveSpeed.max)
    : selectedSpeedDisplay

  return (
    <div className="app-layout">
      <div className="noise-overlay" aria-hidden />
      <div className="sidebar">
        <nav className="nav-rail">
          {([
            { id: 'nav', label: t('nav.navigate'), icon: (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="3 11 22 2 13 21 11 13 3 11" /></svg>
            ) },
            { id: 'connection', label: t('nav.connection'), icon: (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 12.55a11 11 0 0 1 14.08 0" /><path d="M1.42 9a16 16 0 0 1 21.16 0" /><path d="M8.53 16.11a6 6 0 0 1 6.95 0" /><line x1="12" y1="20" x2="12.01" y2="20" /></svg>
            ) },
            { id: 'library', label: t('nav.library'), icon: (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" /></svg>
            ) },
            { id: 'settings', label: t('nav.settings'), icon: (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
            ) },
          ] as const).map((item) => {
            const active = item.id === 'library'
              ? false
              : activePage === item.id
            return (
              <button
                key={item.id}
                className={`nav-rail-btn${active ? ' active' : ''}`}
                title={item.label}
                onClick={() => {
                  if (item.id === 'library') { setOpenLibraryToken((n) => n + 1); return }
                  setActivePage(item.id)
                }}
              >
                <span className="nav-rail-icon">{item.icon}</span>
                <span className="nav-rail-label">{item.label}</span>
              </button>
            )
          })}
        </nav>
        <div className="sidebar-content">
        <DeviceChipRow
          devices={device.connectedDevices}
          runtimes={sim.runtimes}
          onAdd={() => {
            if (device.connectedDevices.length >= 2) {
              setToastMsg(t('device.max_reached'))
              return
            }
            device.scan()
          }}
          onDisconnect={(udid) => { device.disconnect(udid) }}
          onRestoreOne={async (udid) => {
            try {
              await api.restoreSim(udid)
              setToastMsg(t('status.restore_success'))
            } catch (e: any) {
              setToastMsg(e?.message ?? 'restore failed')
            }
          }}
        />
        {activePage === 'settings' && (
          <SettingsPage
            onOpenLogFolder={handleOpenLog}
            onEnableDeveloperMode={async () => {
              const target = device.connectedDevice?.udid
              if (!target) {
                showToast(t('dev_mode.need_device'))
                return
              }
              try {
                await api.amfiRevealDeveloperMode(target)
                showToast(t('dev_mode.reveal_success'))
                await device.scan()
              } catch (err: any) {
                showToast(t('dev_mode.reveal_failed') + (err?.message ? `: ${err.message}` : ''))
              }
            }}
          />
        )}
        <div style={{ display: activePage === 'connection' ? 'block' : 'none' }}>
        <DeviceStatus
          device={deviceStatusDevice}
          devices={deviceStatusDevices}
          isConnected={device.connectedDevice !== null}
          onScan={handleDeviceScan}
          onSelect={handleDeviceSelect}
          onStartWifiTunnel={device.startWifiTunnel}
          onStopTunnel={device.stopTunnel}
          tunnelStatus={device.tunnelStatus}
          tunnels={device.tunnels}
          pinnedUdids={device.pinnedUdids}
          onTogglePin={device.togglePin}
        />
        </div>
        <div style={{ display: activePage === 'nav' ? 'block' : 'none' }}>
        <ControlPanel
          simMode={sim.mode}
          moveMode={sim.moveMode}
          speed={speed}
          isRunning={isRunning}
          isPaused={isPaused}
          currentPosition={currentPos}
          onModeChange={sim.setMode}
          onSpeedChange={handleSpeedChange}
          onMoveModeChange={sim.setMoveMode}
          customSpeedKmh={sim.customSpeedKmh}
          onCustomSpeedChange={sim.setCustomSpeedKmh}
          speedMinKmh={sim.speedMinKmh}
          onSpeedMinChange={sim.setSpeedMinKmh}
          speedMaxKmh={sim.speedMaxKmh}
          onSpeedMaxChange={sim.setSpeedMaxKmh}
          onStart={handleStart}
          onStop={handleStop}
          onPause={handlePause}
          onResume={handleResume}
          onRestore={handleRestore}
          onApplySpeed={handleApplySpeed}
          waypointProgress={sim.waypointProgress}
          onTeleport={handleTeleport}
          onNavigate={handleNavigate}
          onAddressSelect={handleAddressSelect}
          bookmarks={panelBookmarks}
          bookmarkCategories={panelBookmarkCategories}
          bookmarkCategoryColors={panelBookmarkCategoryColors}
          onBookmarkClick={handleBookmarkClick}
          onBookmarkPreview={handleBookmarkPreview}
          onBookmarkAdd={handleBookmarkAdd}
          onBookmarkDelete={handleBookmarkDelete}
          onBookmarkEdit={handleBookmarkEdit}
          onCategoryAdd={handleCategoryAdd}
          onCategoryDelete={handleCategoryDelete}
          onCategoryRename={handleCategoryRename}
          onCategoryRecolor={handleCategoryRecolor}
          onCategoryReorder={handleCategoryReorder}
          onBookmarkReorder={handleBookmarkReorder}
          onCategoryExportGpx={handleCategoryExportGpx}
          onCategoriesExportGpxZip={handleCategoriesExportGpxZip}
          bookmarkShowOnMap={showBookmarkPins}
          onBookmarkShowOnMapChange={setShowBookmarkPins}
          onBookmarkImport={handleBookmarkImport}
          onBookmarkBulkPaste={handleBookmarkBulkPasteOpen}
          bookmarkExportUrl={api.bookmarksExportUrl()}
          savedRoutes={panelSavedRoutes}
          routeCategories={routeCategories}
          onRouteGpxImport={handleGpxImport}
          onRouteGpxExport={handleGpxExport}
          onRoutesImportAll={handleRoutesImportAll}
          routesExportAllUrl={api.exportAllRoutesUrl()}
          onRouteRename={handleRouteRename}
          onRouteDelete={handleRouteDelete}
          onRoutesBulkDelete={handleRoutesBulkDelete}
          onRouteMove={handleRouteMove}
          onRouteLoad={handleRouteLoad}
          onRouteSave={handleRouteSave}
          onRouteCategoryAdd={handleRouteCategoryAdd}
          onRouteCategoryDelete={handleRouteCategoryDelete}
          onRouteCategoryRename={handleRouteCategoryRename}
          onRouteCategoryRecolor={handleRouteCategoryRecolor}
          onRouteCategoryReorder={handleRouteCategoryReorder}
          onRouteReorder={handleRouteReorder}
          randomWalkRadius={randomWalkRadius}
          pauseRandomWalk={sim.pauseRandomWalk}
          onPauseRandomWalkChange={sim.setPauseRandomWalk}
          onRandomWalkRadiusChange={setRandomWalkRadius}
          randomWalkCenterMode={sim.randomWalkCenterMode}
          onRandomWalkCenterModeChange={sim.setRandomWalkCenterMode}
          forwardWalk={sim.forwardWalk}
          onForwardWalkChange={sim.setForwardWalk}
          goldDittoA={goldDittoA}
          onGoldDittoAChange={setGoldDittoA}
          onGoldDittoStart={handleGoldDittoStart}
          goldDittoBusy={goldDittoBusy}
          currentWaypointsCount={sim.waypoints.length}
          loadedRouteName={loadedRouteName}
          straightLine={sim.straightLine}
          onStraightLineChange={sim.setStraightLine}
          keepWaypoints={sim.keepWaypoints}
          onKeepWaypointsChange={sim.setKeepWaypoints}
          routeEngine={sim.routeEngine}
          onRouteEngineChange={sim.setRouteEngine}
          clickToAddWaypoint={clickToAddWaypoint}
          onClickToAddWaypointChange={setClickToAddWaypoint}
          jumpMode={sim.jumpMode}
          onJumpModeChange={sim.setJumpMode}
          jumpPreDelay={sim.jumpPreDelay}
          onJumpPreDelayChange={sim.setJumpPreDelay}
          jumpPostDelay={sim.jumpPostDelay}
          onJumpPostDelayChange={sim.setJumpPostDelay}
          openLibraryToken={openLibraryToken}
          modeExtraSection={modeExtraSection}
        />
        </div>

        </div>
      </div>
      <div className="map-container">
        <EtaBar
          runtimes={sim.runtimes}
          state={sim.status?.state ?? 'idle'}
          progress={sim.progress}
          remainingDistance={sim.status?.distance_remaining ?? 0}
          traveledDistance={sim.status?.distance_traveled ?? 0}
          eta={sim.eta ?? 0}
          legRemainingDistance={sim.status?.leg_distance_remaining}
          legEta={sim.status?.leg_eta_seconds}
        />
        {sim.ddiMounting && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              zIndex: 10000,
              background: 'rgba(20, 22, 32, 0.85)',
              backdropFilter: 'blur(3px)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              pointerEvents: 'auto',
            }}
          >
            <div
              style={{
                background: '#23232a',
                border: '1px solid #3a3a42',
                borderRadius: 8,
                padding: '20px 28px',
                maxWidth: 420,
                textAlign: 'center',
                boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
              }}
            >
              <svg
                width="32" height="32" viewBox="0 0 24 24" fill="none"
                stroke="#6c8cff" strokeWidth="2"
                style={{ animation: 'spin 1s linear infinite', margin: '0 auto 10px' }}
              >
                <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="16" />
              </svg>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>
                {t('ddi.mounting_title')}
              </div>
              <div style={{ fontSize: 12, opacity: 0.75, lineHeight: 1.6 }}>
                {t('ddi.mounting_hint')}
              </div>
              {sim.ddiStage && (() => {
                // Stage labels mapped 1:1 with backend emit() calls in
                // _staged_personalized_mount. Fall back to the raw key
                // if we ever add a stage the UI hasn't learnt yet.
                const stageKey = `ddi.stage_${sim.ddiStage.stage}` as any
                const stageLabel = t(stageKey) || sim.ddiStage.stage
                // Typical total 15-45 s. Use a coarse ETA bucket so it
                // doesn't stress the user with a precise countdown that
                // isn't going to be accurate anyway.
                const elapsed = sim.ddiStage.elapsed
                let etaHint = ''
                if (elapsed < 5) etaHint = t('ddi.eta_starting')
                else if (elapsed < 20) etaHint = t('ddi.eta_continuing')
                else if (elapsed < 60) etaHint = t('ddi.eta_slow')
                else etaHint = t('ddi.eta_very_slow')
                // Rough stage index for progress bar fill.
                const order = ['starting','downloading','verifying','signing','uploading','mounting']
                const idx = Math.max(0, order.indexOf(sim.ddiStage.stage))
                const pct = Math.round(((idx + 1) / order.length) * 100)
                return (
                  <div style={{ marginTop: 14 }}>
                    <div style={{
                      height: 6, background: 'rgba(255,255,255,0.08)',
                      borderRadius: 99, overflow: 'hidden', marginBottom: 8,
                    }}>
                      <div style={{
                        width: `${pct}%`, height: '100%',
                        background: 'linear-gradient(90deg, #6c8cff, #4c6bd9)',
                        transition: 'width 300ms ease-out',
                      }} />
                    </div>
                    <div style={{ fontSize: 11, opacity: 0.85, fontWeight: 600 }}>
                      {stageLabel}
                    </div>
                    <div style={{ fontSize: 10, opacity: 0.55, marginTop: 2 }}>
                      {Math.round(elapsed)}s · {etaHint}
                    </div>
                  </div>
                )
              })()}
            </div>
          </div>
        )}
        {sim.pauseRemaining != null && sim.pauseRemaining > 0 && (
          <div
            style={{
              position: 'absolute',
              top: 38,
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 901,
              background: 'rgba(255, 152, 0, 0.95)',
              color: '#1a1a1a',
              padding: '6px 14px',
              borderRadius: 18,
              fontSize: 12,
              fontWeight: 600,
              boxShadow: '0 2px 8px rgba(0,0,0,0.35)',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="4" width="4" height="16" rx="1" />
              <rect x="14" y="4" width="4" height="16" rx="1" />
            </svg>
            {t('toast.pause_countdown', { n: sim.pauseRemaining })}
          </div>
        )}
        {insertAfterIndex !== null && (
          <div
            style={{
              position: 'absolute',
              top: 38,
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 901,
              background: 'rgba(108, 140, 255, 0.95)',
              color: '#fff',
              padding: '6px 14px',
              borderRadius: 18,
              fontSize: 12,
              fontWeight: 600,
              boxShadow: '0 2px 8px rgba(0,0,0,0.35)',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            <span>
              {t('panel.wp_insert_banner', {
                label: insertAfterIndex === 0
                  ? t('panel.waypoint_start')
                  : `#${insertAfterIndex}`,
              })}
            </span>
            <button
              onClick={cancelInsertMode}
              style={{
                background: 'rgba(255,255,255,0.18)', color: '#fff',
                border: '1px solid rgba(255,255,255,0.4)', borderRadius: 4,
                padding: '2px 8px', fontSize: 11, cursor: 'pointer',
              }}
            >{t('panel.wp_insert_cancel')}</button>
          </div>
        )}
        <MapView
          runtimes={sim.runtimes}
          devices={device.connectedDevices}
          currentPosition={currentPos}
          destination={destPos}
          waypoints={sim.waypoints.map((w, i) => ({ ...w, index: i }))}
          routePath={sim.routePath}
          flowerPreview={flowerPreview}
          randomWalkRadius={
            sim.mode === SimMode.RandomWalk ? randomWalkRadius :
            sim.mode === SimMode.Loop ? wpGenRadius :
            null
          }
          randomWalkCenter={sim.mode === SimMode.RandomWalk ? sim.randomWalkCenter : null}
          randomWalkCenterMode={sim.randomWalkCenterMode}
          onMapClick={handleMapClick}
          onTeleport={handleTeleport}
          onNavigate={handleNavigate}
          onAddBookmark={handleAddBookmark}
          onAddWaypoint={handleAddWaypoint}
          onSetWpAsStart={handleSetWpAsStart}
          onRemoveWaypoint={handleRemoveWaypoint}
          onInsertAfterWp={handleInsertAfterWp}
          insertAfterActive={insertAfterIndex !== null}
          showWaypointOption={sim.mode === SimMode.Loop || sim.mode === SimMode.Flower || sim.mode === SimMode.Navigate}
          deviceConnected={device.connectedDevice !== null}
          onShowToast={showToast}
          userAvatarHtml={avatar.avatarHtml}
          bookmarkPins={bookmarkPins}
          showBookmarkPins={showBookmarkPins}
          onMapReady={(api) => { mapApiRef.current = api }}
          previewPin={previewPin}
          onPreviewPinClear={clearPreviewPin}
          onCoordPreview={handleMapPanOnly}
          recentPlaces={recentPlaces}
          onRecentReFly={(entry) => {
            const isNavigate = entry.kind === 'navigate' || entry.kind === 'coord_navigate'
            if (isNavigate) handleNavigate(entry.lat, entry.lng)
            else handleTeleport(entry.lat, entry.lng)
          }}
          onRecentClear={clearRecentList}
          onOpenLibrary={() => setOpenLibraryToken((t) => t + 1)}
          isRunning={isRunning}
          isPaused={isPaused}
          onStart={handleStart}
          onStop={handleStop}
          onPause={handlePause}
          onResume={handleResume}
          showBulkPasteOnMap={sim.mode === SimMode.Loop || sim.mode === SimMode.Flower}
          onBulkPasteOpen={() => { setRoutePasteText(''); setRoutePasteOpen(true); }}
        />
        {avatar.pickerOpen && (
          <UserAvatarPicker
            avatar={avatar.userAvatar}
            customPng={avatar.customPng}
            onSave={avatar.save}
            onClose={avatar.closePicker}
            onShowToast={showToast}
          />
        )}
        {sim.mode === SimMode.Joystick && (
          <JoystickPad
            direction={joystick.direction}
            intensity={joystick.intensity}
            onMove={joystick.updateFromPad}
            onRelease={() => joystick.updateFromPad(0, 0)}
            active={isRunning}
            hasPosition={!!sim.currentPosition}
          />
        )}
        {addBmDialog && createPortal(
          <div
            onClick={(e) => e.stopPropagation()}
            className="anim-scale-in"
            style={{
              position: 'fixed', top: 60, left: '50%', transform: 'translateX(-50%)',
              zIndex: 1000, background: 'rgba(26, 29, 39, 0.96)',
              backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)',
              border: '1px solid rgba(108, 140, 255, 0.2)',
              borderRadius: 12, padding: 16, width: 300,
              boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65), 0 0 0 1px rgba(255, 255, 255, 0.05) inset',
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{t('bm.add')}</div>
            <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 8 }}>
              {addBmDialog.lat.toFixed(5)}, {addBmDialog.lng.toFixed(5)}
            </div>
            <div style={{ position: 'relative', marginBottom: 8 }}>
              <input
                type="text"
                className="search-input"
                placeholder={addBmDialog.nameResolving ? t('bm.name_resolving') : t('bm.name_placeholder')}
                autoFocus
                value={addBmDialog.name}
                onChange={(e) => setAddBmDialog({ ...addBmDialog, name: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') submitAddBookmark()
                  if (e.key === 'Escape') setAddBmDialog(null)
                }}
                style={{ width: '100%', paddingRight: addBmDialog.nameResolving ? 30 : 8 }}
              />
              {addBmDialog.nameResolving && (
                <span style={{
                  position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
                  fontSize: 10, color: '#9ac0ff', fontFamily: 'monospace',
                  animation: 'pulse 1.2s ease-in-out infinite',
                }}>
                  {t('bm.name_resolving_short')}
                </span>
              )}
              {addBmDialog.countryCode && !addBmDialog.nameResolving && (
                <img
                  src={`https://flagcdn.com/w20/${addBmDialog.countryCode}.png`}
                  alt={addBmDialog.countryCode.toUpperCase()}
                  width={16}
                  height={12}
                  style={{
                    position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
                    borderRadius: 2, boxShadow: '0 0 0 1px rgba(255,255,255,0.15)',
                  }}
                />
              )}
            </div>
            <select
              value={addBmDialog.category}
              onChange={(e) => setAddBmDialog({ ...addBmDialog, category: e.target.value })}
              style={{
                width: '100%', marginBottom: 10, padding: '6px 8px',
                background: '#1e1e22', color: '#e0e0e0', border: '1px solid #444',
                borderRadius: 4, fontSize: 12,
              }}
            >
              {bm.categories.map((c) => (
                <option key={c.id} value={c.name}>{c.name}</option>
              ))}
            </select>
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                className="action-btn primary"
                style={{ flex: 1 }}
                disabled={!addBmDialog.name.trim()}
                onClick={submitAddBookmark}
              >{t('generic.add')}</button>
              <button className="action-btn" onClick={() => setAddBmDialog(null)}>{t('generic.cancel')}</button>
            </div>
          </div>,
          document.body,
        )}
        {bulkPasteOpen && createPortal(
          (() => {
            const { valid, invalidCount, totalLines } = parseBulkPaste(bulkPasteText)
            return (
              <div
                onClick={() => { if (!bulkPasteBusy) setBulkPasteOpen(false) }}
                style={{
                  position: 'fixed', inset: 0, zIndex: 2000,
                  background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                <div
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    width: 460, maxWidth: '92vw', maxHeight: '86vh',
                    display: 'flex', flexDirection: 'column',
                    background: 'rgba(26, 29, 39, 0.96)',
                    border: '1px solid rgba(108, 140, 255, 0.25)', borderRadius: 12,
                    padding: 22, color: '#e8eaf0',
                    boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65)',
                    fontSize: 13,
                  }}
                >
                  <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
                    {t('bm.bulk_paste_title')}
                  </div>
                  <div style={{ fontSize: 11, opacity: 0.65, marginBottom: 10, whiteSpace: 'pre-line', lineHeight: 1.5 }}>
                    {t('bm.bulk_paste_hint')}
                  </div>
                  <textarea
                    value={bulkPasteText}
                    onChange={(e) => setBulkPasteText(e.target.value)}
                    placeholder="25.0478 121.5319 台北車站&#10;24.1456 120.6839 台中"
                    style={{
                      width: '100%', boxSizing: 'border-box',
                      minHeight: 160, maxHeight: 240, resize: 'vertical',
                      background: 'rgba(10, 12, 18, 0.7)',
                      border: '1px solid rgba(108, 140, 255, 0.3)',
                      borderRadius: 6, color: '#e8eaf0',
                      padding: '8px 10px', fontFamily: 'monospace', fontSize: 12, lineHeight: 1.5,
                      outline: 'none',
                    }}
                  />
                  <div style={{ fontSize: 11, opacity: 0.7, marginTop: 8 }}>
                    {totalLines > 0 && t('bm.bulk_paste_stats')
                      .replace('{total}', String(totalLines))
                      .replace('{valid}', String(valid.length))
                      .replace('{invalid}', String(invalidCount))}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10 }}>
                    <span style={{ fontSize: 12, opacity: 0.75 }}>{t('bm.bulk_paste_category')}:</span>
                    <select
                      value={bulkPasteCategory}
                      onChange={(e) => setBulkPasteCategory(e.target.value)}
                      className="search-input"
                      style={{ flex: 1, padding: '4px 8px', fontSize: 12 }}
                    >
                      {bm.categories.map((c) => (
                        <option key={c.id} value={c.name}>{c.name}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ display: 'flex', gap: 8, marginTop: 16, justifyContent: 'flex-end' }}>
                    <button
                      onClick={() => { if (!bulkPasteBusy) { setBulkPasteOpen(false); setBulkPasteText('') } }}
                      disabled={bulkPasteBusy}
                      style={{
                        padding: '6px 14px', fontSize: 12, cursor: bulkPasteBusy ? 'not-allowed' : 'pointer',
                        background: 'transparent', color: '#9499ac',
                        border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6,
                        opacity: bulkPasteBusy ? 0.6 : 1,
                      }}
                    >{t('generic.cancel')}</button>
                    <button
                      onClick={submitBulkPaste}
                      disabled={bulkPasteBusy || valid.length === 0}
                      style={{
                        padding: '6px 14px', fontSize: 12, fontWeight: 600,
                        cursor: (bulkPasteBusy || valid.length === 0) ? 'not-allowed' : 'pointer',
                        background: valid.length === 0 ? 'rgba(108,140,255,0.3)' : '#6c8cff',
                        color: '#fff',
                        border: 'none', borderRadius: 6,
                        opacity: bulkPasteBusy ? 0.6 : 1,
                      }}
                    >
                      {bulkPasteBusy ? '...' : `${t('bm.bulk_paste_submit')} (${valid.length})`}
                    </button>
                  </div>
                </div>
              </div>
            )
          })(),
          document.body,
        )}
        {wpFlyConfirm && createPortal(
          <div
            onClick={() => setWpFlyConfirm(null)}
            style={{
              position: 'fixed', inset: 0, zIndex: 2000,
              background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: 360, maxWidth: '92vw',
                background: 'rgba(26, 29, 39, 0.96)',
                border: '1px solid rgba(108, 140, 255, 0.25)', borderRadius: 12,
                padding: 22, color: '#e8eaf0',
                boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65)',
                fontSize: 13,
              }}
            >
              <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
                {t('panel.wp_fly_title')}
              </div>
              <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 6, lineHeight: 1.6 }}>
                {t('panel.wp_fly_hint')}
              </div>
              <div style={{
                fontFamily: 'monospace', fontSize: 13,
                padding: '8px 10px', marginBottom: 4,
                background: 'rgba(10, 12, 18, 0.5)',
                border: '1px solid rgba(108, 140, 255, 0.2)',
                borderRadius: 6,
              }}>
                {wpFlyConfirm.lat.toFixed(6)}, {wpFlyConfirm.lng.toFixed(6)}
              </div>
              <div style={{ fontSize: 11, opacity: 0.55, marginBottom: 16 }}>
                {t('panel.wp_fly_keep_mode')}
              </div>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
                <button
                  onClick={() => setWpFlyConfirm(null)}
                  style={{
                    padding: '6px 14px', fontSize: 12, cursor: 'pointer',
                    background: 'transparent', color: '#9499ac',
                    border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6,
                  }}
                >{t('generic.cancel')}</button>
                {wpFlyConfirm.index > 0 ? (
                  <button
                    onClick={async () => {
                      const idx = wpFlyConfirm.index
                      setWpFlyConfirm(null)
                      await handleSetWpAsStart(idx)
                    }}
                    style={{
                      padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
                      background: '#6c8cff', color: '#fff',
                      border: 'none', borderRadius: 6,
                    }}
                    title={t('panel.waypoints_set_as_start')}
                  >{t('panel.wp_fly_set_as_start')}</button>
                ) : (
                  // index 0 IS the start — no rotation possible. Fall back
                  // to the plain teleport so clicking the start coord still
                  // lets the user re-align the iPhone to it.
                  <button
                    onClick={confirmWpFly}
                    style={{
                      padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
                      background: '#6c8cff', color: '#fff',
                      border: 'none', borderRadius: 6,
                    }}
                  >{t('panel.wp_fly_confirm')}</button>
                )}
              </div>
            </div>
          </div>,
          document.body,
        )}
        {routeLoadConfirm && createPortal(
          <div
            onClick={() => setRouteLoadConfirm(null)}
            style={{
              position: 'fixed', inset: 0, zIndex: 2000,
              background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: 380, maxWidth: '92vw',
                background: 'rgba(26, 29, 39, 0.96)',
                border: '1px solid rgba(108, 140, 255, 0.25)', borderRadius: 12,
                padding: 22, color: '#e8eaf0',
                boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65)',
                fontSize: 13,
              }}
            >
              <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
                {t('panel.route_load_title')}
              </div>
              {routeLoadConfirm.name && (
                <div style={{
                  fontSize: 13, marginBottom: 8, padding: '6px 10px',
                  background: 'rgba(108, 140, 255, 0.1)',
                  border: '1px solid rgba(108, 140, 255, 0.2)', borderRadius: 6,
                }}>
                  {routeLoadConfirm.name}
                </div>
              )}
              <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 6, lineHeight: 1.6 }}>
                {t('panel.route_load_hint', { n: routeLoadConfirm.waypoints.length })}
              </div>
              {routeLoadConfirm.waypoints.length > 0 && (
                <div style={{
                  fontFamily: 'monospace', fontSize: 12,
                  padding: '8px 10px', marginBottom: 16,
                  background: 'rgba(10, 12, 18, 0.5)',
                  border: '1px solid rgba(108, 140, 255, 0.2)', borderRadius: 6,
                }}>
                  {t('panel.route_load_start')} {routeLoadConfirm.waypoints[0].lat.toFixed(6)}, {routeLoadConfirm.waypoints[0].lng.toFixed(6)}
                </div>
              )}
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
                <button
                  onClick={() => setRouteLoadConfirm(null)}
                  style={{
                    padding: '6px 14px', fontSize: 12, cursor: 'pointer',
                    background: 'transparent', color: '#9499ac',
                    border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6,
                  }}
                >{t('generic.cancel')}</button>
                <button
                  onClick={() => void confirmRouteLoad(false)}
                  style={{
                    padding: '6px 14px', fontSize: 12, cursor: 'pointer',
                    background: 'transparent', color: '#e8eaf0',
                    border: '1px solid rgba(108, 140, 255, 0.5)', borderRadius: 6,
                  }}
                >{t('panel.route_load_show_only')}</button>
                <button
                  onClick={() => void confirmRouteLoad(true)}
                  style={{
                    padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
                    background: '#6c8cff', color: '#fff',
                    border: 'none', borderRadius: 6,
                  }}
                >{t('panel.route_load_fly_start')}</button>
              </div>
            </div>
          </div>,
          document.body,
        )}
        {routePasteOpen && createPortal(
          (() => {
            const { valid, invalidCount, totalLines } = parseRoutePaste(routePasteText)
            return (
              <div
                onClick={() => setRoutePasteOpen(false)}
                style={{
                  position: 'fixed', inset: 0, zIndex: 2000,
                  background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                <div
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    width: 460, maxWidth: '92vw', maxHeight: '86vh',
                    display: 'flex', flexDirection: 'column',
                    background: 'rgba(26, 29, 39, 0.96)',
                    border: '1px solid rgba(108, 140, 255, 0.25)', borderRadius: 12,
                    padding: 22, color: '#e8eaf0',
                    boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65)',
                    fontSize: 13,
                  }}
                >
                  <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
                    {t('panel.route_paste_title')}
                  </div>
                  <div style={{ fontSize: 11, opacity: 0.65, marginBottom: 10, whiteSpace: 'pre-line', lineHeight: 1.5 }}>
                    {t('panel.route_paste_hint')}
                  </div>
                  <textarea
                    value={routePasteText}
                    onChange={(e) => setRoutePasteText(e.target.value)}
                    placeholder="25.0478 121.5319&#10;25.0500 121.5400&#10;25.0530 121.5500"
                    style={{
                      width: '100%', boxSizing: 'border-box',
                      minHeight: 180, maxHeight: 280, resize: 'vertical',
                      background: 'rgba(10, 12, 18, 0.7)',
                      border: '1px solid rgba(108, 140, 255, 0.3)',
                      borderRadius: 6, color: '#e8eaf0',
                      padding: '8px 10px', fontFamily: 'monospace', fontSize: 12, lineHeight: 1.5,
                      outline: 'none',
                    }}
                  />
                  <div style={{ fontSize: 11, opacity: 0.7, marginTop: 8 }}>
                    {totalLines > 0 && t('panel.route_paste_stats')
                      .replace('{total}', String(totalLines))
                      .replace('{valid}', String(valid.length))
                      .replace('{invalid}', String(invalidCount))}
                  </div>
                  <div style={{ fontSize: 11, opacity: 0.6, marginTop: 4 }}>
                    {t('panel.route_paste_start_hint')}
                  </div>
                  <div style={{ display: 'flex', gap: 8, marginTop: 16, justifyContent: 'space-between', alignItems: 'center' }}>
                    <button
                      onClick={async () => {
                        try {
                          const text = await navigator.clipboard.readText()
                          if (text) setRoutePasteText(text)
                        } catch {
                          showToast(t('panel.route_paste_clipboard_blocked'))
                        }
                      }}
                      title={t('panel.route_paste_from_clipboard_tooltip')}
                      style={{
                        padding: '6px 12px', fontSize: 12, cursor: 'pointer',
                        background: 'rgba(108, 140, 255, 0.18)', color: '#9bb0ff',
                        border: '1px solid rgba(108, 140, 255, 0.4)', borderRadius: 6,
                        display: 'inline-flex', alignItems: 'center', gap: 5,
                      }}
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="9" y="2" width="6" height="4" rx="1"/>
                        <path d="M9 4H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-3"/>
                        <path d="M9 12h6M9 16h4"/>
                      </svg>
                      {t('panel.route_paste_from_clipboard')}
                    </button>
                    <div style={{ display: 'flex', gap: 8 }}>
                    <button
                      onClick={() => { setRoutePasteOpen(false); setRoutePasteText('') }}
                      style={{
                        padding: '6px 14px', fontSize: 12, cursor: 'pointer',
                        background: 'transparent', color: '#9499ac',
                        border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6,
                      }}
                    >{t('generic.cancel')}</button>
                    <button
                      onClick={submitRoutePaste}
                      disabled={valid.length === 0}
                      style={{
                        padding: '6px 14px', fontSize: 12, fontWeight: 600,
                        cursor: valid.length === 0 ? 'not-allowed' : 'pointer',
                        background: valid.length === 0 ? 'rgba(108,140,255,0.3)' : '#6c8cff',
                        color: '#fff',
                        border: 'none', borderRadius: 6,
                      }}
                    >{`${t('panel.route_paste_submit')} (${valid.length})`}</button>
                    </div>
                  </div>
                </div>
              </div>
            )
          })(),
          document.body,
        )}
        {sim.error && (
          <div
            style={{
              position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
              zIndex: 2000, background: '#e53935', color: '#fff', padding: '8px 20px',
              borderRadius: 6, fontSize: 13, boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              cursor: 'pointer', maxWidth: '80%', textAlign: 'center',
            }}
            onClick={sim.clearError}
          >
            {sim.error}
          </div>
        )}
        <StatusBar
          runtimes={sim.runtimes}
          devices={device.connectedDevices}
          isConnected={device.connectedDevice !== null}
          deviceName={device.connectedDevice?.name ?? ''}
          iosVersion={device.connectedDevice?.ios_version ?? ''}
          currentPosition={currentPos}
          speed={displaySpeed}
          mode={sim.mode}
          cooldown={cooldown}
          cooldownEnabled={cooldownEnabled}
          onToggleCooldown={handleToggleCooldown}
          onRestore={handleRestore}
          onOpenLog={handleOpenLog}
          onOpenSettings={handleOpenSettings}
          dualDevice={device.connectedDevices.length >= 2}
          countryCode={locMeta.countryCode}
          cityName={locMeta.cityName}
          weatherCode={locMeta.weatherCode}
          tempC={locMeta.tempC}
          timezoneZone={locMeta.timezoneZone}
          gmtOffsetSeconds={locMeta.gmtOffsetSeconds}
          onOpenAvatarPicker={avatar.togglePicker}
          onLocatePcFly={handleTeleport}
          onLocatePcPanOnly={handleMapPanOnly}
        />

        {toastMsg && (
          <div
            key={toastMsg}
            className="anim-fade-slide-down"
            style={{
              position: 'fixed',
              top: 72,
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 1500,
              background: 'rgba(26, 29, 39, 0.92)',
              backdropFilter: 'blur(10px)',
              WebkitBackdropFilter: 'blur(10px)',
              color: '#fff',
              padding: '10px 18px',
              borderRadius: 10,
              fontSize: 13,
              fontWeight: 500,
              letterSpacing: '-0.005em',
              boxShadow: '0 10px 32px rgba(12, 18, 40, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.06) inset',
              border: '1px solid rgba(108, 140, 255, 0.3)',
              maxWidth: '70vw',
              textAlign: 'center',
              whiteSpace: 'pre-line',
              lineHeight: 1.5,
            }}
          >
            {toastMsg}
          </div>
        )}
      </div>
    </div>
  )
}

export default App
