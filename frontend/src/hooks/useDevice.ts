import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import {
  listDevices, connectDevice, disconnectDevice,
  wifiTunnelStartAndConnect, wifiTunnelStatus, wifiTunnelStop, wifiTunnelDiscover,
  type TunnelInfo,
} from '../services/api'
import type { WsMessage } from './useWebSocket'
import { readSavedIps, removeSavedIpByUdid, upsertSavedIp, writeSavedIps } from '../utils/savedIps'
import { classifyPinAttempt } from '../utils/autoConnect'

export interface DeviceInfo {
  udid: string
  name: string
  ios_version: string
  connection_type: string
  is_connected: boolean
  // iOS 16+ Developer Mode toggle state. null = unknown (iOS <16, query
  // failed, or device not yet connected). Used to decide whether to show
  // the "Reveal Developer Mode option" button.
  developer_mode_enabled?: boolean | null
  // WiFi only: the RemotePairing port that actually completed the handshake.
  port?: number
}

export type WsSubscribe = (fn: (m: WsMessage) => void) => () => void

export function useDevice(subscribe?: WsSubscribe) {
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [connectedDevice, setConnectedDevice] = useState<DeviceInfo | null>(null)

  // React to real-time device state broadcasts via the subscribe callback.
  // See useWebSocket.ts for the rationale vs the old useState pattern.
  useEffect(() => {
    if (!subscribe) return
    return subscribe((msg) => {
      if (msg.type === 'device_disconnected') {
        // Group mode: only mark the specific udid disconnected when provided;
        // fall back to clearing all for legacy single-device disconnect events.
        const udid = msg.data?.udid
        const udids: string[] = Array.isArray(msg.data?.udids) ? msg.data.udids : (udid ? [udid] : [])
        if (udids.length === 0) {
          setConnectedDevice(null)
          setDevices((prev) => prev.map((d) => ({ ...d, is_connected: false })))
          // Also clear the WiFi tunnel list so the 連線 page doesn't keep
          // showing a device that was just disconnected (issue: right-click
          // disconnect left the tunnel chip showing "still connected").
          setTunnels([])
        } else {
          setDevices((prev) => prev.map((d) => udids.includes(d.udid) ? { ...d, is_connected: false } : d))
          setTunnels((prev) => prev.filter((tn) => !udids.includes(tn.udid)))
          // DON'T null out connectedDevice here. The authoritative refresh
          // below (listDevices) will pick a surviving device to promote
          // so downstream UI (MapView / StatusBar) doesn't flash
          // 'No device' in dual-device mode when only one was unplugged.
        }
        // Re-fetch so the sidebar list and metadata stay in sync with the
        // backend, AND promote a surviving connected device as the new
        // active one when the old primary was the one unplugged. This
        // fixes the bug where unplugging A (primary) in dual-device mode
        // made the UI think no device was connected even though B was
        // still alive.
        listDevices().then((list) => {
          setDevices(list)
          setConnectedDevice((prev) => {
            // Keep the current one if it's still connected.
            if (prev && list.some((d) => d.udid === prev.udid && d.is_connected)) return prev
            // Otherwise promote the first surviving connected device.
            return list.find((d) => d.is_connected) ?? null
          })
        }).catch(() => {})
      } else if (msg.type === 'device_connected') {
        // Re-fetch list so the newly-connected device appears with correct metadata.
        listDevices().then((list) => {
          setDevices(list)
          // If nothing is currently set as the active device, promote the
          // newly-connected one so the bottom panel switches off NODEVICE
          // without the user having to press the USB button.
          const udid = msg.data?.udid
          const match = udid ? list.find((d) => d.udid === udid && d.is_connected) : null
          setConnectedDevice((prev) => prev ?? match ?? list.find((d) => d.is_connected) ?? null)
        }).catch(() => {})
      } else if (msg.type === 'device_reconnected') {
        listDevices().then((list) => {
          setDevices(list)
          const udid = msg.data?.udid
          const match = udid ? list.find((d) => d.udid === udid) : null
          setConnectedDevice(match ?? list.find((d) => d.is_connected) ?? null)
        }).catch(() => {})
      }
    })
  }, [subscribe])
  const [scanning, setScanning] = useState(false)

  const scan = useCallback(async () => {
    setScanning(true)
    try {
      const result = await listDevices()
      const list: DeviceInfo[] = Array.isArray(result) ? result : []
      setDevices(list)
      const active = list.find((d) => d.is_connected) ?? null
      if (active) {
        setConnectedDevice(active)
      } else if (list.length === 1) {
        // Auto-connect when exactly one device is found
        try {
          await connectDevice(list[0].udid)
          const refreshed = await listDevices()
          const rList: DeviceInfo[] = Array.isArray(refreshed) ? refreshed : []
          setDevices(rList)
          setConnectedDevice(rList.find((d) => d.udid === list[0].udid) ?? list[0])
        } catch {
          setConnectedDevice(null)
        }
      } else {
        setConnectedDevice(null)
      }
      return list
    } catch (err) {
      console.error('Failed to scan devices:', err)
      return []
    } finally {
      setScanning(false)
    }
  }, [])

  const connect = useCallback(
    async (udid: string) => {
      try {
        await connectDevice(udid)
        const refreshed = await listDevices()
        const list: DeviceInfo[] = Array.isArray(refreshed) ? refreshed : []
        setDevices(list)
        const active = list.find((d) => d.udid === udid) ?? null
        setConnectedDevice(active)
        return active
      } catch (err) {
        console.error('Failed to connect device:', err)
        throw err
      }
    },
    [],
  )

  const disconnect = useCallback(
    async (udid: string) => {
      try {
        await disconnectDevice(udid)
        const refreshed = await listDevices()
        const list: DeviceInfo[] = Array.isArray(refreshed) ? refreshed : []
        setDevices(list)
        // Only the named device was disconnected — DON'T blanket-null the
        // active device. In dual/triple mode that made the whole UI flip to
        // "NO device" even though the other iPhones were still connected.
        // Keep the current active device if it survived; otherwise promote
        // any remaining connected one; null only when nothing is left.
        setConnectedDevice((prev) => {
          if (prev && prev.udid !== udid && list.some((d) => d.udid === prev.udid && d.is_connected)) return prev
          return list.find((d) => d.is_connected) ?? null
        })
        setTunnels((prev) => prev.filter((tn) => tn.udid !== udid))
      } catch (err) {
        console.error('Failed to disconnect device:', err)
        throw err
      }
    },
    [],
  )

  // v0.2.83: WiFi tunnel state went from a singleton to a per-device list.
  // Each connected iOS 17+ WiFi device gets its own runner on the backend;
  // `tunnels` mirrors that list. `tunnelStatus` is kept as a derived
  // singleton (mirrors first tunnel) for any leftover single-tunnel callers
  // until they migrate.
  const [tunnels, setTunnels] = useState<TunnelInfo[]>([])
  // Memoized so the derived object only changes identity when `tunnels`
  // does (the hook's return object below depends on it).
  const tunnelStatus = useMemo(() => tunnels.length > 0
    ? { running: true, rsd_address: tunnels[0].rsd_address, rsd_port: tunnels[0].rsd_port }
    : { running: false }, [tunnels])

  // ── Pin & auto-reconnect (issue #33) ──────────────────────────────
  // A pinned device keeps trying to reconnect on its own after the
  // backend watchdog gives up (tunnel_lost). The backend already retries
  // 3x with backoff for transient blips; this covers the longer outages
  // (phone opened late, left the WiFi for a while) the user has to fix by
  // hand today. State is persisted so a pin survives an app restart.
  const PIN_KEY = 'locwarp.tunnel.pinned'
  const readPinned = (): string[] => {
    try {
      const arr = JSON.parse(localStorage.getItem(PIN_KEY) || '[]')
      return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []
    } catch { return [] }
  }
  const [pinnedUdids, setPinnedUdids] = useState<string[]>(readPinned)
  const pinnedRef = useRef<string[]>(pinnedUdids)
  pinnedRef.current = pinnedUdids
  const tunnelsRef = useRef<TunnelInfo[]>(tunnels)
  tunnelsRef.current = tunnels
  // All three maps below are keyed by lowercased udid (see schedulePinReconnect
  // for why the key must be normalized).
  const pinRetryTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const pinRetryFailures = useRef<Record<string, number>>({})
  // Consecutive reschedule count per device, used to back off the retry
  // interval for devices that stay offline a long time (see schedulePinReconnect).
  const pinRetryRounds = useRef<Record<string, number>>({})
  // Set after startWifiTunnel / stopTunnel are defined below; the retry
  // loop calls through the refs so we avoid a definition-order cycle.
  const startWifiTunnelRef = useRef<((ip: string, port?: number, udidHint?: string, portHints?: number[]) => Promise<any>) | null>(null)
  const stopTunnelRef = useRef<((udid?: string) => Promise<void>) | null>(null)

  const clearPinRetry = useCallback((udid: string) => {
    const key = udid.toLowerCase()
    const tmr = pinRetryTimers.current[key]
    if (tmr) { clearTimeout(tmr); delete pinRetryTimers.current[key] }
    delete pinRetryFailures.current[key]
    delete pinRetryRounds.current[key]
  }, [])

  const readSavedEntryFor = (udid: string): { ip: string; port: number; ports?: number[] } | null => {
    try {
      const arr = JSON.parse(localStorage.getItem('locwarp.tunnel.savedips') || '[]')
      if (!Array.isArray(arr)) return null
      // Case-insensitive: savedips entries can be written under a
      // different-case udid than the one the retry loop is chasing (see
      // schedulePinReconnect). A strict match here silently skipped the
      // direct-reconnect attempt on every round and fell straight through
      // to the heavy discover fallback, turning the ~45s full-network-scan
      // cadence into every ~15s.
      const udidLc = udid.toLowerCase()
      const hit = arr.find((e: any) => e && typeof e.udid === 'string' && e.udid.toLowerCase() === udidLc && typeof e.ip === 'string')
      if (hit) {
        const ports = Array.isArray(hit.ports)
          ? hit.ports.map(Number).filter((p: number) => Number.isInteger(p) && p > 0 && p <= 65535)
          : undefined
        return { ip: hit.ip, port: Number(hit.port) || 49152, ...(ports?.length ? { ports } : {}) }
      }
    } catch { /* ignore */ }
    return null
  }

  const schedulePinReconnect = useCallback((udid: string, delayMs = 5000) => {
    // Normalize the dedup key to lowercase up front. Callers pass different
    // casings for the same physical device — App.tsx's cold-start retry uses
    // the pinned-list casing, while the tunnel_lost WS handler uses whatever
    // case the backend's RSD peer_info happened to report. If the timer /
    // failure-count maps were keyed by the raw udid, those two callers could
    // each arm their own independent 15s loop for the same iPhone, doubling
    // network load and fighting each other over the connection.
    const udidLc = udid.toLowerCase()
    if (pinRetryTimers.current[udidLc]) return // already scheduled
    const attempt = async () => {
      delete pinRetryTimers.current[udidLc]
      // Stop if the user unpinned, or the tunnel already came back.
      if (!pinnedRef.current.some((u) => u.toLowerCase() === udidLc)) return
      if (tunnelsRef.current.some((tn) => tn.udid.toLowerCase() === udidLc)) return

      // Everything below must fall through to the reschedule block in
      // `finally` unless we've confirmed OUR target device is the one that
      // just connected. Wrapping the whole body in try/finally means an
      // unexpected thrown error (not just the handled failure branches)
      // can never silently kill the retry loop — setTimeout callbacks are
      // fire-and-forget, so an unhandled rejection here would otherwise
      // vanish with no reschedule and no visible error.
      let reconnected = false
      try {
        const entry = readSavedEntryFor(udid)
        const failures = pinRetryFailures.current[udidLc] ?? 0
        if (entry && failures < 2) {
          // startWifiTunnel's udidHint is only a HINT for the backend's
          // candidate search, not a guarantee — start-and-connect can
          // resolve successfully against a completely different device
          // than the one we asked for. Treating "the call didn't throw"
          // as "our device is back" was the root cause of the retry loop
          // going permanently silent: a stranger (or another pinned
          // phone) connecting on this endpoint used to hit `return` here,
          // skipping the reschedule below forever. We check the resolved
          // identity via classifyPinAttempt instead. A null/undefined
          // ref (e.g. startWifiTunnel not wired up yet) also resolves to
          // 'failed' rather than being mistaken for success.
          let info: { udid: string } | null | undefined
          try {
            info = entry.ports?.length
              ? await startWifiTunnelRef.current?.(entry.ip, entry.port, udid, entry.ports)
              : await startWifiTunnelRef.current?.(entry.ip, entry.port, udid)
          } catch {
            info = null
          }
          const outcome = classifyPinAttempt({
            targetUdid: udid,
            resultUdid: info?.udid,
            pinnedUdids: pinnedRef.current,
          })
          if (outcome === 'reconnected') {
            reconnected = true
            return // success path clears the timer via startWifiTunnel
          }
          if (outcome === 'stranger' && info?.udid) {
            // Connected, but to a device nobody pinned — undo and scrub
            // the stale saved IP so future attempts stop chasing it.
            await stopTunnelRef.current?.(info.udid)
            writeSavedIps(removeSavedIpByUdid(readSavedIps(), info.udid))
          }
          if (outcome === 'stranger' || outcome === 'other-pinned') {
            // The (ip, port) we just dialed for `udid` resolved to a
            // DIFFERENT device — proof that udid's own savedips entry is
            // stale (the endpoint moved on: DHCP lease change, port
            // rebind, etc). Scrub it too, not just the stranger/other-
            // pinned entry above. Left in place, readSavedEntryFor(udid)
            // keeps returning this same dead endpoint every ~15s round,
            // and connect_wifi_tunnel's backend path calls disconnect()
            // for any already-connected udid before reconnecting —
            // disconnect() calls location_service.clear()
            // (backend/core/device_manager.py:766-767) — so each retry
            // round kicks whoever now legitimately owns this endpoint and
            // wipes their running location simulation. Clearing it here
            // makes the next round's readSavedEntryFor return null and
            // fall straight to the discover branch below, which re-finds
            // udid's real, current endpoint instead of repeating this.
            writeSavedIps(removeSavedIpByUdid(readSavedIps(), udid))
          }
          // 'other-pinned' reached a keeper for its own owner — leave its
          // tunnel up (no stopTunnel call: it's the user's own device and
          // staying connected is desired). Either way (including
          // 'failed'), OUR target still isn't back, so this attempt is a
          // miss; fall through to retry.
          pinRetryFailures.current[udidLc] = failures + 1
        } else {
          // Two direct failures (or nothing saved) — the iPhone likely
          // rebound its RemotePairing port or moved to a new DHCP lease.
          // Re-discover once and try the fresh endpoints; identity is
          // verified after connect (drop anything not pinned).
          try {
            const dres = await wifiTunnelDiscover()
            for (const d of dres?.devices || []) {
              if (tunnelsRef.current.some((tn) => tn.udid.toLowerCase() === udidLc)) break
              let info: { udid: string } | null | undefined
              try {
                const ports = Array.isArray(d.ports)
                  ? d.ports.map(Number).filter((p: number) => Number.isInteger(p) && p > 0 && p <= 65535)
                  : []
                info = ports.length > 0
                  ? await startWifiTunnelRef.current?.(
                    String(d.ip), Number(d.port) || 49152, udid, ports,
                  )
                  : await startWifiTunnelRef.current?.(
                    String(d.ip), Number(d.port) || 49152, udid,
                  )
              } catch {
                info = null // try next candidate
              }
              if (!info) continue
              const outcome = classifyPinAttempt({
                targetUdid: udid,
                resultUdid: info.udid,
                pinnedUdids: pinnedRef.current,
              })
              if (outcome === 'reconnected') {
                reconnected = true
                return // reconnected our target
              }
              if (outcome === 'stranger') {
                // Reached an unpinned stranger — undo and scrub. Use the
                // hook's own stopTunnel (not the raw API call) so the
                // `tunnels` state list drops the entry too; otherwise the
                // kicked device leaves a zombie chip in the panel until
                // the next tunnel_lost/device_disconnected broadcast.
                await stopTunnelRef.current?.(info.udid)
                writeSavedIps(removeSavedIpByUdid(readSavedIps(), info.udid))
              }
              if (outcome === 'stranger' || outcome === 'other-pinned') {
                // Same reasoning as the direct-reconnect branch above:
                // this candidate resolved to someone other than `udid`,
                // so scrub udid's own savedips entry defensively (if any
                // still points at this or a similarly stale endpoint) —
                // otherwise a future round could rediscover and redial
                // the same wrong endpoint, kicking that device's tunnel
                // and clearing its location simulation.
                writeSavedIps(removeSavedIpByUdid(readSavedIps(), udid))
              }
              // A different pinned device ('other-pinned') is a keeper;
              // keep looking for ours among the remaining candidates.
            }
          } catch { /* discover failed — retry cycle continues below */ }
          pinRetryFailures.current[udidLc] = 0 // next cycle starts with the saved entry again
        }
      } finally {
        if (
          !reconnected &&
          pinnedRef.current.some((u) => u.toLowerCase() === udidLc) &&
          !tunnelsRef.current.some((tn) => tn.udid.toLowerCase() === udidLc)
        ) {
          // Back off for devices that stay offline a long time. Cold-start
          // retries (App.tsx) now arm this loop for pinned devices that were
          // ALREADY offline at launch, not just ones that dropped mid-session
          // — and a phone left locked in a pocket can stay offline for a
          // long while. Without backing off, every ~45s cycle (2 direct
          // attempts + 1 discover round, at 15s each) re-runs the discover
          // fallback's full mDNS + /24 + full-range scan forever. After 8
          // rounds (~3 minutes) with no luck, stretch the interval to 60s.
          // Reset to 0 the moment the device reconnects (see clearPinRetry).
          const round = (pinRetryRounds.current[udidLc] ?? 0) + 1
          pinRetryRounds.current[udidLc] = round
          const interval = round > 8 ? 60000 : 15000
          pinRetryTimers.current[udidLc] = setTimeout(attempt, interval)
        }
      }
    }
    pinRetryTimers.current[udidLc] = setTimeout(attempt, delayMs)
  }, [])

  const PIN_IP_MAP_KEY = 'locwarp.tunnel.pin_ip_map'

  const togglePin = useCallback((udid: string) => {
    setPinnedUdids((prev) => {
      const next = prev.includes(udid) ? prev.filter((u) => u !== udid) : [...prev, udid]
      try { localStorage.setItem(PIN_KEY, JSON.stringify(next)) } catch { /* ignore */ }
      if (!next.includes(udid)) {
        clearPinRetry(udid)
        // Remove IP mapping when unpinning so the device is no longer
        // auto-connected on next startup (issue #35).
        try {
          const map = JSON.parse(localStorage.getItem(PIN_IP_MAP_KEY) || '{}')
          delete map[udid]
          localStorage.setItem(PIN_IP_MAP_KEY, JSON.stringify(map))
        } catch { /* ignore */ }
      } else {
        // Save last known IP when pinning so startup auto-connect can
        // filter to pinned devices only (issue #35).
        const entry = readSavedEntryFor(udid)
        if (entry) {
          try {
            const map = JSON.parse(localStorage.getItem(PIN_IP_MAP_KEY) || '{}')
            map[udid] = `${entry.ip}:${entry.port}`
            localStorage.setItem(PIN_IP_MAP_KEY, JSON.stringify(map))
          } catch { /* ignore */ }
        }
      }
      return next
    })
  }, [clearPinRetry])

  // Drive pin retries off the tunnel lifecycle events. Kept separate from
  // the panel-state handler above so ordering / deps stay simple.
  useEffect(() => {
    if (!subscribe) return
    return subscribe((msg) => {
      if (msg.type === 'tunnel_lost') {
        const udid = msg.data?.udid
        // Case-insensitive: see the comment in schedulePinReconnect for why
        // (pair-record / RSD peer_info case can differ from the saved pin).
        if (udid && pinnedRef.current.some((u) => u.toLowerCase() === String(udid).toLowerCase())) {
          schedulePinReconnect(udid)
        }
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
    })
  }, [subscribe, schedulePinReconnect, clearPinRetry])

  // React to backend tunnel lifecycle events so the DeviceStatus panel
  // doesn't keep showing a dead tunnel as connected when the iPhone
  // leaves the WiFi network. Without this, the `tunnels` list is only
  // mutated by explicit Start / Stop button clicks — issue #29.
  useEffect(() => {
    if (!subscribe) return
    return subscribe((msg) => {
      if (msg.type === 'tunnel_lost') {
        const udid = msg.data?.udid
        if (udid) {
          setTunnels((prev) => prev.filter((tn) => tn.udid !== udid))
          setDevices((prev) => prev.map((d) =>
            d.udid === udid && d.connection_type === 'Network'
              ? { ...d, is_connected: false }
              : d,
          ))
        } else {
          // No udid in the payload — fall back to a full re-query so
          // we never leave a phantom tunnel chip in the panel.
          wifiTunnelStatus().then((res) => {
            setTunnels(Array.isArray(res?.tunnels) ? res.tunnels : [])
          }).catch(() => setTunnels([]))
        }
      } else if (msg.type === 'tunnel_recovered') {
        const udid = msg.data?.udid
        const rsd_address = msg.data?.rsd_address
        const rsd_port = msg.data?.rsd_port
        if (udid && rsd_address && typeof rsd_port === 'number') {
          setTunnels((prev) => {
            const filtered = prev.filter((tn) => tn.udid !== udid)
            return [...filtered, { udid, rsd_address, rsd_port }]
          })
        }
      }
    })
  }, [subscribe])

  const startWifiTunnel = useCallback(
    async (ip: string, port = 49152, udidHint?: string, portHints?: number[]) => {
      try {
        // Keep the legacy three-argument call shape when no hints are
        // supplied; callers and existing integrations treat the fourth
        // argument as optional.
        const res = portHints && portHints.length > 0
          ? await wifiTunnelStartAndConnect(ip, port, udidHint, portHints)
          : await wifiTunnelStartAndConnect(ip, port, udidHint)
        // The backend can re-scan and succeed on a different port than the
        // remembered one. Persist the actual handshake port for next launch.
        const usedPort = Number(res.port) > 0 ? Number(res.port) : port
        const info: DeviceInfo = {
          udid: res.udid,
          name: res.name,
          ios_version: res.ios_version,
          connection_type: 'Network',
          is_connected: true,
          port: usedPort,
        }
        setConnectedDevice(info)
        setDevices((prev) => {
          const filtered = prev.filter((d) => d.udid !== info.udid)
          return [...filtered, info]
        })
        setTunnels((prev) => {
          const filtered = prev.filter((tn) => tn.udid !== res.udid)
          return [...filtered, {
            udid: res.udid,
            rsd_address: res.rsd_address,
            rsd_port: res.rsd_port,
          }]
        })
        // Persist every successful tunnel into savedips, regardless of
        // who initiated it (manual button, launch auto-connect, mDNS
        // discover-and-connect). Without this, an iPhone that was
        // connected via auto-discovery never gets remembered, and the
        // next launch only auto-connects whichever iPhone the user once
        // manually clicked through. v0.2.110 bug surfaced when a user
        // with two iPhones only had one of them in savedips.
        try {
          const raw = localStorage.getItem('locwarp.tunnel.savedips') || '[]'
          const list = (() => {
            try { return JSON.parse(raw) as Array<{ ip: string; port: number; udid?: string; name?: string; lastUsed: number }> }
            catch { return [] }
          })()
          const baseList = Array.isArray(list) ? list : []
          // Dedup by both (ip, port) AND by udid — covers the case where
          // an iPhone reconnects on a NEW DHCP-assigned IP. Without the
          // udid dedup we'd accumulate stale IPs for the same device.
          const filtered = baseList.filter((e) =>
            e && !(e.ip === ip && (e.port === port || e.port === usedPort))
            && !(res.udid && e.udid === res.udid)
          )
          // Persist the device name too so the panel can keep showing the
          // real phone name after a WiFi drop instead of a raw UDID
          // (issue #33).
          const next = [{ ip, port: usedPort, udid: res.udid, name: res.name, lastUsed: Date.now() }, ...filtered].slice(0, 5)
          localStorage.setItem('locwarp.tunnel.savedips', JSON.stringify(next))
        } catch { /* storage disabled */ }
        // A successful connect clears any pending pin-retry for this device.
        clearPinRetry(res.udid)
        return info
      } catch (err) {
        console.error('WiFi tunnel failed:', err)
        throw err
      }
    },
    [],
  )
  // Expose the latest startWifiTunnel to the pin-retry loop without making
  // it a hook dependency (the callback is stable, deps: []).
  startWifiTunnelRef.current = startWifiTunnel

  const checkTunnelStatus = useCallback(async () => {
    try {
      const res = await wifiTunnelStatus()
      setTunnels(Array.isArray(res?.tunnels) ? res.tunnels : [])
      return res
    } catch {
      setTunnels([])
      return { tunnels: [], running: false }
    }
  }, [])

  // udid: stop one specific tunnel; omit to stop all.
  const stopTunnel = useCallback(async (udid?: string) => {
    try {
      await wifiTunnelStop(udid)
      if (udid) {
        setTunnels((prev) => prev.filter((tn) => tn.udid !== udid))
      } else {
        setTunnels([])
      }
    } catch (err) {
      console.error('Failed to stop tunnel:', err)
    }
  }, [])
  // Expose the latest stopTunnel to the pin-retry loop without making it
  // a hook dependency (the callback is stable, deps: []).
  stopTunnelRef.current = stopTunnel

  // Group-mode derived state: every device in `devices` marked is_connected.
  // `primaryDevice` sticks to whichever device we picked first; we only
  // promote a new one when the current sticky primary is no longer in the
  // connected slice. Without stickiness, listDevices()'s order on a
  // mid-session reconnect can swap primary back to the just-rejoined
  // device, which then receives the auto-sync replay (a fresh sim from
  // its current position) and the frontend lets that REPLAY's events
  // through the udid filter, overwriting the surviving device's polyline
  // and "瞬移回起點 / 慢慢走回起點" on screen. Sticky primary keeps the
  // surviving device in charge so the rejoining one's replay stays
  // filtered out and invisible until the user explicitly chooses to
  // switch.
  const connectedDevices: DeviceInfo[] = useMemo(
    () => devices.filter((d) => d.is_connected), [devices])
  const [stickyPrimaryUdid, setStickyPrimaryUdid] = useState<string | null>(null)
  useEffect(() => {
    if (connectedDevices.length === 0) {
      if (stickyPrimaryUdid !== null) setStickyPrimaryUdid(null)
      return
    }
    if (stickyPrimaryUdid && connectedDevices.some((d) => d.udid === stickyPrimaryUdid)) {
      return
    }
    setStickyPrimaryUdid(connectedDevices[0].udid)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [devices])
  const primaryDevice: DeviceInfo | null = useMemo(
    () => devices.find((d) => d.udid === stickyPrimaryUdid && d.is_connected) ?? null,
    [devices, stickyPrimaryUdid])

  // Memoize the public API object so consumers (App.tsx useCallbacks that
  // list `device` as a dep) only re-bind when device state actually changes,
  // not on every render of the host component.
  return useMemo(() => ({
    devices, connectedDevice, scanning, scan, connect, disconnect,
    startWifiTunnel, checkTunnelStatus, stopTunnel, tunnelStatus, tunnels,
    connectedDevices, primaryDevice,
    pinnedUdids, togglePin, schedulePinReconnect,
  }), [
    devices, connectedDevice, scanning, scan, connect, disconnect,
    startWifiTunnel, checkTunnelStatus, stopTunnel, tunnelStatus, tunnels,
    connectedDevices, primaryDevice,
    pinnedUdids, togglePin, schedulePinReconnect,
  ])
}
