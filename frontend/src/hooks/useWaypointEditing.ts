import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from '../services/api'
import { parseCoord } from '../utils/coords'
import { useSimulation, SimMode } from './useSimulation'
import { useDevice } from './useDevice'
import { useT } from '../i18n'
import type { RecentKind } from '../services/api'

type Sim = ReturnType<typeof useSimulation>
type Device = ReturnType<typeof useDevice>
type Translate = ReturnType<typeof useT>

// Leaflet wraps the world horizontally at very low zoom levels; clicking on
// a "second copy" of a country yields lng outside [-180, 180]. Backend's
// pydantic TeleportRequest bounds lng to [-180, 180] so the raw click
// would 422. Normalize at the handler entry so every downstream call sees
// a single canonical coordinate.
export const normalizeLng = (lng: number): number => {
  const n = ((lng + 180) % 360 + 360) % 360 - 180
  // ((180 + 180) % 360 + 360) % 360 - 180 == -180, but 180 is also valid.
  // Keep +180 if the input was exactly +180.
  return lng === 180 ? 180 : n
}
export const clampLat = (lat: number): number => Math.max(-90, Math.min(90, lat))

// Waypoint-list editing for the Loop / Flower modes: map-click add,
// insert-after mode, move / remove / trim, random generation, the
// fly-to-waypoint confirm dialog, and the route bulk-paste dialog. All
// mutations go through sim.setWaypoints so single- and dual-device modes
// stay in sync with the backend's seg_idx math.
export function useWaypointEditing({ sim, device, t, showToast, pushRecent, clickToAddWaypoint }: {
  sim: Sim
  device: Device
  t: Translate
  showToast: (msg: string, ms?: number) => void
  pushRecent: (lat: number, lng: number, kind: RecentKind, name?: string) => Promise<void>
  clickToAddWaypoint: boolean
}) {
  const [wpGenRadius, setWpGenRadius] = useState(300)
  const [wpGenCount, setWpGenCount] = useState(5)

  const generateWaypoints = useCallback((radius: number, count: number) => {
    if (!sim.currentPosition) {
      alert(t('toast.no_position_random'))
      return
    }
    const { lat, lng } = sim.currentPosition
    const latScale = 111320
    const lngScale = 111320 * Math.cos((lat * Math.PI) / 180)

    type Pt = { lat: number; lng: number; theta?: number }
    const pts: Pt[] = []
    for (let i = 0; i < count; i++) {
      const r = radius * Math.sqrt(Math.random())
      const theta = Math.random() * 2 * Math.PI
      pts.push({
        lat: lat + (r * Math.cos(theta)) / latScale,
        lng: lng + (r * Math.sin(theta)) / lngScale,
        theta,
      })
    }

    // Nearest-neighbor from current position → shorter total path
    const remaining = [...pts]
    const ordered: Pt[] = []
    let cx = lat, cy = lng
    while (remaining.length) {
      let bestIdx = 0, bestD = Infinity
      for (let i = 0; i < remaining.length; i++) {
        const dx = (remaining[i].lat - cx) * latScale
        const dy = (remaining[i].lng - cy) * lngScale
        const d = dx * dx + dy * dy
        if (d < bestD) { bestD = d; bestIdx = i }
      }
      const [next] = remaining.splice(bestIdx, 1)
      ordered.push(next)
      cx = next.lat; cy = next.lng
    }

    // Seed the list with the current position as index 0 so the start button
    // doesn't need to inject it later (and can't double-inject on re-click).
    sim.setWaypoints([
      { lat, lng },
      ...ordered.map(({ lat, lng }) => ({ lat, lng })),
    ])
  }, [sim, t])

  const handleGenerateRandomWaypoints = useCallback(() => {
    generateWaypoints(wpGenRadius, wpGenCount)
  }, [generateWaypoints, wpGenRadius, wpGenCount])

  const handleGenerateAllRandom = useCallback(() => {
    const radius = Math.floor(50 + Math.random() * 950)  // 50–1000 m
    const count = Math.floor(3 + Math.random() * 8)       // 3–10 點
    setWpGenRadius(radius)
    setWpGenCount(count)
    generateWaypoints(radius, count)
  }, [generateWaypoints])

  // Insert-after-waypoint mode: when set, the next map click drops a new
  // waypoint immediately AFTER the chosen index instead of appending to
  // the end. Activated from the waypoint left-click menu (map) or the
  // fly-confirm dialog (left side). Cleared by ESC, by clicking the
  // banner's cancel, or after one successful insert.
  const [insertAfterIndex, setInsertAfterIndex] = useState<number | null>(null)
  const handleInsertAfterWp = useCallback((index: number) => {
    setInsertAfterIndex(index)
  }, [])
  const cancelInsertMode = useCallback(() => setInsertAfterIndex(null), [])

  // ESC cancels insert mode anywhere in the app — same affordance as
  // every dialog.
  useEffect(() => {
    if (insertAfterIndex === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setInsertAfterIndex(null)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [insertAfterIndex])

  const handleMapClick = useCallback((lat: number, lng: number) => {
    const nlat = clampLat(lat)
    const nlng = normalizeLng(lng)
    // Priority 1: insert-after mode. One-shot — clears itself after the
    // splice so the next plain click goes back to the default behaviour
    // (no-op or click-to-add-waypoint, depending on the toggle).
    if (insertAfterIndex !== null) {
      const idx = insertAfterIndex
      // Always update the local list immediately so the UI shows the
      // new waypoint without waiting for the backend round-trip.
      sim.setWaypoints((prev: any[]) => {
        const safeIdx = Math.min(Math.max(idx, 0), prev.length - 1)
        const target = safeIdx + 1
        const next = [...prev]
        next.splice(target, 0, { lat: nlat, lng: nlng })
        return next
      })
      // If a multi-stop / loop is currently running, also push the
      // splice into every connected device's engine so each iPhone
      // walks the new waypoint as part of the active route (no need
      // to Stop+Start). When inserted in a future leg the device
      // continues to that leg and visits the new wp in line; when
      // inserted in a past / current leg the new wp is recorded for
      // the route list but the iPhone keeps walking forward without
      // backtracking. See SimulationEngine.live_insert_waypoint.
      const isRouteMode = sim.mode === SimMode.Loop
      if (isRouteMode && sim.status?.running) {
        const udids = device.connectedDevices.map((d) => d.udid)
        if (udids.length > 0) {
          void Promise.allSettled(
            udids.map((u) => api.insertWaypoint(idx, nlat, nlng, u)),
          )
        } else {
          void api.insertWaypoint(idx, nlat, nlng).catch(() => {})
        }
      }
      setInsertAfterIndex(null)
      return
    }
    // When the "left-click to add waypoint" toggle is on AND we're in a
    // waypoint-based mode, append to the waypoint list. Otherwise a map
    // click is a no-op (teleport / navigate live on right-click menu).
    if (!clickToAddWaypoint) return
    if (sim.mode !== SimMode.Loop && sim.mode !== SimMode.Flower) return
    sim.setWaypoints((prev: any[]) => {
      if (prev.length === 0 && sim.currentPosition) {
        return [
          { lat: sim.currentPosition.lat, lng: sim.currentPosition.lng },
          { lat: nlat, lng: nlng },
        ]
      }
      return [...prev, { lat: nlat, lng: nlng }]
    })
  }, [clickToAddWaypoint, insertAfterIndex, sim])

  const handleAddWaypoint = useCallback((lat: number, lng: number) => {
    // Seed the list with the current device position as the implicit start
    // point on the first add. This keeps backend route and UI list aligned
    // so waypoint-progress highlighting indexes correctly, and removes the
    // "start button injects current pos every click" footgun.
    const nlat = clampLat(lat)
    const nlng = normalizeLng(lng)
    sim.setWaypoints((prev: any[]) => {
      if (prev.length === 0 && sim.currentPosition) {
        return [
          { lat: sim.currentPosition.lat, lng: sim.currentPosition.lng },
          { lat: nlat, lng: nlng },
        ]
      }
      return [...prev, { lat: nlat, lng: nlng }]
    })
  }, [sim])

  const handleClearWaypoints = useCallback(() => {
    sim.setWaypoints([])
  }, [sim])

  const handleRemoveWaypoint = useCallback((index: number) => {
    sim.setWaypoints((prev: any[]) => prev.filter((_: any, i: number) => i !== index))
  }, [sim])

  // Move a waypoint up / down inside the Loop / MultiStop list. waypoints[0]
  // is the implicit start (current device position when the first add fired),
  // so it's pinned — we never let the user shuffle index 0, and other rows
  // can't be moved into position 0. Same idempotent pattern as the remove
  // handler: swap two entries inside the immutable list.
  const handleMoveWaypoint = useCallback((index: number, direction: -1 | 1) => {
    sim.setWaypoints((prev: any[]) => {
      const target = index + direction
      if (index <= 0 || target <= 0) return prev
      if (index >= prev.length || target >= prev.length) return prev
      const next = prev.slice()
      const tmp = next[index]
      next[index] = next[target]
      next[target] = tmp
      return next
    })
  }, [sim])

  // Trim the waypoint list so the chosen index becomes the new start.
  // Everything before `index` is dropped — the iPhone won't walk back
  // through them on the next Start press. Concretely: setting #9 as
  // start on a 1..15 route gives 9 → 10 → ... → 15 (and Loop wraps
  // back to 9, not to 1). User asked for trim (not rotate) so a
  // pause-and-resume-from-#9 flow doesn't re-walk #1..#8 at the end.
  const handleSetWpAsStart = useCallback(async (index: number) => {
    const wps = sim.waypoints
    if (index <= 0 || index >= wps.length) return
    const trimmed = wps.slice(index)
    sim.setWaypoints(trimmed)
    const start = trimmed[0]
    sim.setCurrentPosition({ lat: start.lat, lng: start.lng })
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length > 0) {
      try { await sim.teleportAll(udids, start.lat, start.lng) } catch { /* ignore */ }
    }
    void pushRecent(start.lat, start.lng, 'coord_teleport')
  }, [sim, device, pushRecent])

  // Teleport to a waypoint from inside the Loop / MultiStop list. We
  // go around sim.teleport (which flips sim.mode to Teleport and would
  // therefore wipe waypoints the next time the user clicks the Loop /
  // MultiStop mode tab). Talk directly to sim.teleportAll / api.
  // teleport so the current mode and the entire waypoint list stay
  // intact while the iPhone jumps to the chosen point.
  const [wpFlyConfirm, setWpFlyConfirm] = useState<{ lat: number; lng: number; index: number } | null>(null)
  const confirmWpFly = useCallback(async () => {
    if (!wpFlyConfirm) return
    const { lat, lng } = wpFlyConfirm
    sim.setCurrentPosition({ lat, lng })
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length > 0) {
      try { await sim.teleportAll(udids, lat, lng) } catch { /* ignore */ }
    }
    void pushRecent(lat, lng, 'coord_teleport')
    setWpFlyConfirm(null)
  }, [wpFlyConfirm, sim, device, pushRecent])

  // Waypoint list starts collapsed so a long route doesn't bury the speed /
  // action controls; the user expands it when they want to edit points.
  const [wpCollapsed, setWpCollapsed] = useState(true)

  // Route bulk-paste: parse a textarea of "lat lng [name]" lines into a
  // waypoint list for Loop / MultiStop. Current device position (if
  // any) is prepended as waypoint[0] so the backend's seg_idx math
  // lines up with the UI, matching handleAddWaypoint's contract.
  // Works identically in single- and dual-device modes because sim.
  // setWaypoints feeds both the global state and any fanout call site.
  const [routePasteOpen, setRoutePasteOpen] = useState(false)
  const [routePasteText, setRoutePasteText] = useState('')
  const parseRoutePaste = useCallback((raw: string): { valid: Array<{ lat: number; lng: number }>; invalidCount: number; totalLines: number } => {
    const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0)
    const valid: Array<{ lat: number; lng: number }> = []
    let invalidCount = 0
    for (const line of lines) {
      const c = parseCoord(line)
      if (!c) { invalidCount++; continue }
      valid.push({ lat: clampLat(c.lat), lng: normalizeLng(c.lng) })
    }
    return { valid, invalidCount, totalLines: lines.length }
  }, [])
  const submitRoutePaste = useCallback(async () => {
    const { valid } = parseRoutePaste(routePasteText)
    if (valid.length === 0) {
      showToast(t('panel.route_paste_empty'))
      return
    }
    // First pasted coord = route start. Teleport iPhone there so
    // waypoints[0] lines up with current GPS, BUT don't go through
    // handleTeleport / sim.teleport — those flip sim.mode back to
    // Teleport, which would then clear waypoints the moment the user
    // clicks Loop / MultiStop in the sidebar. Use the raw API + a
    // direct setCurrentPosition so the mode the user set (Loop /
    // MultiStop) stays intact.
    const first = valid[0]
    sim.setCurrentPosition({ lat: first.lat, lng: first.lng })
    const udids = device.connectedDevices.map((d) => d.udid)
    if (udids.length > 0) {
      try { await sim.teleportAll(udids, first.lat, first.lng) } catch { /* ignore */ }
    }
    sim.setWaypoints(valid)
    setRoutePasteOpen(false)
    setRoutePasteText('')
    showToast(t('panel.route_paste_done').replace('{count}', String(valid.length)))
  }, [routePasteText, parseRoutePaste, sim, device, t, showToast])

  return useMemo(() => ({
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
  }), [
    wpGenRadius, wpGenCount,
    handleGenerateRandomWaypoints, handleGenerateAllRandom,
    insertAfterIndex, handleInsertAfterWp, cancelInsertMode,
    handleMapClick, handleAddWaypoint, handleClearWaypoints,
    handleRemoveWaypoint, handleMoveWaypoint, handleSetWpAsStart,
    wpFlyConfirm, confirmWpFly, wpCollapsed,
    routePasteOpen, routePasteText, parseRoutePaste, submitRoutePaste,
  ])
}
