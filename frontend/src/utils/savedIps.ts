// Pure helpers around the locwarp.tunnel.savedips localStorage list
// (max 10 entries, newest first). Extracted so App.tsx / useDevice can share
// one implementation and the logic stays unit-testable.

export interface SavedIpEntry {
  ip: string
  port: number
  udid?: string
  name?: string
  ports?: number[]
  // First three octets, matching the existing /24 tunnel-discovery hint.
  // Missing values are derived from the saved IP when reading old entries.
  prefix?: string
  lastUsed: number
}

export const SAVED_IPS_KEY = 'locwarp.tunnel.savedips'
const MAX_ENTRIES = 10

export function prefixOfIp(ip: string): string | undefined {
  const parts = ip.split('.')
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part) || Number(part) > 255)) {
    return undefined
  }
  return parts.slice(0, 3).map(Number).join('.')
}

export function readSavedIps(storage: Pick<Storage, 'getItem'> = localStorage): SavedIpEntry[] {
  try {
    const parsed = JSON.parse(storage.getItem(SAVED_IPS_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((e) => e && typeof e.ip === 'string' && typeof e.port === 'number')
      .map((e) => ({
        ...e,
        prefix: typeof e.prefix === 'string' ? e.prefix : prefixOfIp(e.ip),
      }))
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
  const udidLc = entry.udid?.toLowerCase()
  const sameNetworkDevice = (candidate: SavedIpEntry) =>
    Boolean(udidLc)
    && candidate.udid?.toLowerCase() === udidLc
    && (candidate.prefix || '') === (entry.prefix || '')
  const existingForDevice = udidLc ? list.find(sameNetworkDevice) : undefined
  const normalizedEntry = {
    ...entry,
    ...(entry.name || !existingForDevice?.name ? {} : { name: existingForDevice.name }),
  }
  const filtered = list.filter((e) =>
    e
    && !(e.ip === entry.ip && e.port === entry.port && (e.prefix || '') === (entry.prefix || ''))
    && !sameNetworkDevice(e),
  )
  return [normalizedEntry, ...filtered].slice(0, MAX_ENTRIES)
}

export function savedIpsForPrefix<T extends { prefix?: string }>(list: T[], prefix: string | null): T[] {
  if (!prefix) return list
  // Prefix-less entries can come from manually edited storage or malformed
  // historical records. Keep them eligible so an uncertain entry cannot
  // prevent a reconnect attempt.
  return list.filter((entry) => !entry.prefix || entry.prefix === prefix)
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

export function removeSavedIpForNetwork(list: SavedIpEntry[], udid: string, prefix?: string): SavedIpEntry[] {
  const udidLc = udid.toLowerCase()
  return list.filter((entry) => !(
    typeof entry.udid === 'string'
    && entry.udid.toLowerCase() === udidLc
    && (entry.prefix || '') === (prefix || '')
  ))
}
