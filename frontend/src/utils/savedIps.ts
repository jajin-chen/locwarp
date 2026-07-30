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
  // Case-insensitive: callers pass udids sourced from different backend
  // responses (usbmuxd device list vs. RSD peer_info at tunnel-connect
  // time) that can differ in case for the same physical device — see the
  // identical rationale in useDevice.ts's readSavedEntryFor. A strict
  // comparison here would silently fail to scrub a stale entry whose
  // casing doesn't match the udid the caller is chasing.
  const udidLc = udid.toLowerCase()
  return list.filter((e) => !(typeof e.udid === 'string' && e.udid.toLowerCase() === udidLc))
}
