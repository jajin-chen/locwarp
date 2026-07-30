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

// Which pinned UDIDs still need a reconnect retry armed after a launch
// auto-connect pass — i.e. pinned but not among the tunnels that actually
// came up. Case-insensitive: `liveTunnelUdids` comes from the backend's
// live tunnel status (RSD peer_info casing), which can differ from the
// casing the udid was originally pinned/saved under.
export function pinnedUdidsNeedingRetry(opts: {
  pinnedUdids: string[]
  liveTunnelUdids: string[]
}): string[] {
  const liveLc = new Set(opts.liveTunnelUdids.map((u) => u.toLowerCase()))
  return opts.pinnedUdids.filter((udid) => !liveLc.has(udid.toLowerCase()))
}

// Outcome of one pin-retry attempt against a tunnel start call whose udid
// argument is only a HINT to the backend's candidate search — the call can
// resolve successfully against a device other than the one requested. The
// retry loop (useDevice's schedulePinReconnect) must classify what actually
// came back instead of treating "didn't throw" as "our device reconnected",
// or it will stop rescheduling the moment a resolve happens to land on a
// different phone.
export type PinAttemptOutcome = 'reconnected' | 'stranger' | 'other-pinned' | 'failed'

export function classifyPinAttempt(opts: {
  targetUdid: string
  resultUdid?: string | null
  pinnedUdids: string[]
}): PinAttemptOutcome {
  const { targetUdid, resultUdid, pinnedUdids } = opts
  if (!resultUdid) return 'failed'
  const resultLc = resultUdid.toLowerCase()
  if (resultLc === targetUdid.toLowerCase()) return 'reconnected'
  const pinnedLc = pinnedUdids.map((u) => u.toLowerCase())
  return pinnedLc.includes(resultLc) ? 'other-pinned' : 'stranger'
}
