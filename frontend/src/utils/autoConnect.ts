// Candidate list for launch auto-connect. With pins set we now still run
// discovery (the iPhone's RemotePairing port changes on every reboot /
// WiFi rejoin, so saved endpoints alone go stale) — identity is enforced
// AFTER connect by checking the handshake udid against the pin list.

export interface TunnelCandidate {
  ip: string
  port: number
  udid?: string
}

const DEFAULT_MAX = 3

export function buildAutoConnectCandidates(opts: {
  saved: TunnelCandidate[]
  discovered: Array<{ ip: string; port: number }>
  pinnedUdids: string[]
  alreadyTunneled: ReadonlySet<string>
  max?: number
}): TunnelCandidate[] {
  const { saved, discovered, pinnedUdids, alreadyTunneled } = opts
  const max = opts.max ?? DEFAULT_MAX
  const hasPins = pinnedUdids.length > 0
  // Case-insensitive match: backend compares UDIDs case-insensitively (see
  // backend/api/device.py), and pair-record / RSD peer_info casing can
  // differ from what was originally saved. A strict `includes` here made a
  // legitimately pinned phone with different-case udid look unpinned and
  // get filtered out of its own auto-connect candidate list.
  const pinnedUdidsLc = pinnedUdids.map((u) => u.toLowerCase())
  const savedFiltered = hasPins
    ? saved.filter((e) => e.udid && pinnedUdidsLc.includes(e.udid.toLowerCase()))
    : saved

  const seen = new Set<string>()
  const out: TunnelCandidate[] = []
  const add = (ip: string, port: number, udid?: string) => {
    const key = `${ip}:${port}`
    if (seen.has(key) || alreadyTunneled.has(key)) return
    seen.add(key)
    out.push({ ip, port, udid })
  }

  for (const e of savedFiltered) add(e.ip, e.port, e.udid)
  for (const d of discovered) add(d.ip, d.port, undefined)
  return out.slice(0, max)
}
