// Candidate list for launch auto-connect. With pins set we now still run
// discovery (the iPhone's RemotePairing port changes on every reboot /
// WiFi rejoin, so saved endpoints alone go stale) — identity is enforced
// AFTER connect by checking the handshake udid against the pin list.

export interface TunnelCandidate {
  ip: string
  port: number
  ports?: number[]
  udid?: string
}

const DEFAULT_MAX = 3

export function buildAutoConnectCandidates(opts: {
  saved: TunnelCandidate[]
  discovered: Array<{ ip: string; port: number; ports?: number[] }>
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

  // Group by IP before applying the device cap. A saved endpoint can be
  // stale while discovery reports several fresh ports for that same phone;
  // treating each port as a separate candidate would spend the max-3 slots
  // on one device and starve other phones. Keep the saved UDID as the
  // identity hint, but let current discovery evidence lead the port order.
  type GroupedCandidate = TunnelCandidate & {
    _savedPorts: number[]
    _discoveredPorts: number[]
    _portsExplicit?: boolean
  }
  const byIp = new Map<string, GroupedCandidate>()
  const discoveredIps = new Set(discovered.map((entry) => entry.ip))
  const normalizePorts = (entry: TunnelCandidate): number[] => {
    const sourcePorts = [entry.port, ...(entry.ports || [])]
      .map((p) => Number(p))
      .filter((p) => Number.isInteger(p) && p > 0 && p <= 65535)
    return sourcePorts.filter((port, index) => sourcePorts.indexOf(port) === index)
  }
  const add = (entry: TunnelCandidate, source: 'saved' | 'discovered', udid?: string) => {
    const ports = normalizePorts(entry)
    if (ports.length === 0) return
    const existing = byIp.get(entry.ip)
    if (!existing) {
      byIp.set(entry.ip, {
        ip: entry.ip,
        port: ports[0],
        udid,
        _savedPorts: source === 'saved' ? ports : [],
        _discoveredPorts: source === 'discovered' ? ports : [],
        _portsExplicit: Boolean(entry.ports?.length),
      })
      return
    }

    const target = source === 'saved' ? existing._savedPorts : existing._discoveredPorts
    for (const port of ports) {
      if (!target.includes(port)) target.push(port)
    }
    // Saved entries are the only source of an identity hint. Keep the first
    // one when several stale records share an IP.
    if (!existing.udid && udid) existing.udid = udid
    existing._portsExplicit = existing._portsExplicit || Boolean(entry.ports?.length)
  }

  for (const entry of savedFiltered) add(entry, 'saved', entry.udid)
  for (const entry of discovered) add(entry, 'discovered')

  return [...byIp.values()]
    .map(({ _savedPorts, _discoveredPorts, _portsExplicit, ...candidate }) => {
      // A current discovery record is fresher than a saved endpoint. Make
      // its primary/hints the backend's first choices, then append saved
      // ports as fallback without dropping the saved UDID.
      const merged = [..._discoveredPorts, ..._savedPorts]
        .filter((port, index, ports) => ports.indexOf(port) === index)
      if (merged.length > 0) {
        candidate.port = merged[0]
        if (_portsExplicit || merged.length > 1) candidate.ports = merged
        else delete candidate.ports
      }
      return candidate
    })
    .filter((candidate) => {
      const ports = candidate.ports || [candidate.port]
      // A stop/status snapshot identifies a whole device by any active
      // endpoint. Once the merged group contains one active endpoint, do
      // not retry another port for that same IP in this pass.
      return !ports.some((port) => alreadyTunneled.has(`${candidate.ip}:${port}`))
    })
    // Fresh discovery evidence wins the device cap over saved-only stale
    // addresses.
    .sort((a, b) => Number(discoveredIps.has(b.ip)) - Number(discoveredIps.has(a.ip)))
    .slice(0, max)
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
