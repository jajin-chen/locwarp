// @vitest-environment happy-dom
//
// Integration test for the pin auto-reconnect loop in useDevice.
// Only the HTTP layer (services/api) is mocked — the hook, the
// classify/savedips helpers, and the timer scheduling all run for real
// under vitest fake timers.
//
// The core regression guarded here: startWifiTunnel's udid argument is
// only a HINT, so a retry attempt can "succeed" against a different
// device. The loop must treat that as a miss and KEEP rescheduling —
// the old behaviour (treat "didn't throw" as success) silently killed
// the loop and left the pinned iPhone offline for hours.
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

const api = vi.hoisted(() => ({
  listDevices: vi.fn(async () => []),
  connectDevice: vi.fn(async () => ({})),
  disconnectDevice: vi.fn(async () => ({})),
  wifiConnect: vi.fn(async () => ({})),
  wifiScan: vi.fn(async () => []),
  wifiTunnelStartAndConnect: vi.fn(),
  wifiTunnelStatus: vi.fn(async () => ({ tunnels: [], running: false })),
  wifiTunnelStop: vi.fn(async () => ({ status: 'stopped' })),
  wifiTunnelDiscover: vi.fn(async (): Promise<{ devices: Array<{ ip: string; port: number }> }> => ({ devices: [] })),
}))

vi.mock('../../services/api', () => api)

import { useDevice } from '../useDevice'
import type { WsMessage } from '../useWebSocket'

const TARGET = 'TARGET-UDID'
const OTHER_PINNED = 'OTHER-PINNED-UDID'

const tunnelResult = (udid: string) => ({
  udid,
  name: `iPhone ${udid}`,
  ios_version: '17.5',
  rsd_address: 'fd00::1',
  rsd_port: 1234,
})

const readSavedIps = (): Array<{ ip: string; port: number; udid?: string }> =>
  JSON.parse(localStorage.getItem('locwarp.tunnel.savedips') || '[]')

const seedStorage = (pinned: string[]) => {
  localStorage.setItem('locwarp.tunnel.pinned', JSON.stringify(pinned))
  localStorage.setItem('locwarp.tunnel.savedips', JSON.stringify([
    { ip: '192.168.1.10', port: 50000, udid: TARGET, lastUsed: 1 },
  ]))
}

// Node 25 ships a global `localStorage` that is non-functional unless the
// process is started with --localstorage-file, and it shadows the DOM
// environment's Storage. Stub a real in-memory Storage for the hook to use.
const makeStorage = (): Storage => {
  const map = new Map<string, string>()
  return {
    get length() { return map.size },
    key: (i: number) => [...map.keys()][i] ?? null,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => { map.set(k, String(v)) },
    removeItem: (k: string) => { map.delete(k) },
    clear: () => { map.clear() },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', makeStorage())
  vi.clearAllMocks()
  api.wifiTunnelStatus.mockResolvedValue({ tunnels: [], running: false })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('schedulePinReconnect', () => {
  test('keeps rescheduling after resolving onto a stranger device (stall-bug guard)', async () => {
    seedStorage([TARGET])
    // Every attempt lands on a device nobody pinned.
    api.wifiTunnelStartAndConnect.mockResolvedValue(tunnelResult('STRANGER'))
    api.wifiTunnelDiscover.mockResolvedValue({ devices: [{ ip: '192.168.1.99', port: 50001 }] })

    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })

    // Round 1 (after the initial 5s delay): direct reconnect via savedips.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(1)
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledWith('192.168.1.10', 50000, TARGET)
    // The stranger's tunnel is torn down and BOTH stale savedips entries
    // (the stranger's and the target's own dead endpoint) are scrubbed.
    expect(api.wifiTunnelStop).toHaveBeenCalledWith('STRANGER')
    expect(readSavedIps()).toEqual([])

    // Round 2 (15s later): savedips is empty now, so the loop falls back
    // to discovery and dials the fresh endpoint. This round happening at
    // all is the regression guard — the pre-fix loop died in round 1.
    await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
    expect(api.wifiTunnelDiscover).toHaveBeenCalledTimes(1)
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(2)
    expect(api.wifiTunnelStartAndConnect).toHaveBeenLastCalledWith('192.168.1.99', 50001, TARGET)

    // Round 3: still going.
    await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(3)
  })

  test('keeps rescheduling after resolving onto another pinned device, without kicking it', async () => {
    seedStorage([TARGET, OTHER_PINNED])
    api.wifiTunnelStartAndConnect.mockResolvedValue(tunnelResult(OTHER_PINNED))
    api.wifiTunnelDiscover.mockResolvedValue({ devices: [] })

    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })

    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(1)
    // The other pinned phone is the user's own device — its tunnel stays up.
    expect(api.wifiTunnelStop).not.toHaveBeenCalled()
    // But the target's stale savedips entry is scrubbed so future rounds
    // stop redialing an endpoint that now belongs to someone else.
    expect(readSavedIps().filter((e) => e.udid === TARGET)).toEqual([])

    // The loop must survive and run another round.
    await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
    expect(api.wifiTunnelStartAndConnect.mock.calls.length
      + api.wifiTunnelDiscover.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  test('stops retrying once the target device actually reconnects', async () => {
    seedStorage([TARGET])
    api.wifiTunnelStartAndConnect.mockResolvedValue(tunnelResult(TARGET))

    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })

    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(1)
    expect(api.wifiTunnelStop).not.toHaveBeenCalled()

    // No further attempts, even far past the retry interval.
    await act(async () => { await vi.advanceTimersByTimeAsync(120000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(1)
    expect(api.wifiTunnelDiscover).not.toHaveBeenCalled()
  })

  test('does nothing for a device that is not pinned', async () => {
    seedStorage([])

    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })

    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(api.wifiTunnelStartAndConnect).not.toHaveBeenCalled()
    expect(api.wifiTunnelDiscover).not.toHaveBeenCalled()
  })
})

describe('startWifiTunnel', () => {
  test('persists the backend actual handshake port in savedips', async () => {
    api.wifiTunnelStartAndConnect.mockResolvedValue({
      ...tunnelResult(TARGET),
      port: 61234,
    })

    const { result } = renderHook(() => useDevice())
    await act(async () => {
      await result.current.startWifiTunnel(
        '192.168.1.10',
        49152,
        TARGET,
        [49152, 61234],
      )
    })

    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledWith(
      '192.168.1.10', 49152, TARGET, [49152, 61234],
    )
    expect(readSavedIps()[0]).toMatchObject({
      ip: '192.168.1.10',
      port: 61234,
      udid: TARGET,
    })
  })
})


describe('host tunnel failure', () => {
  test('blocks all pins and cold-start rearming, preserves healthy tunnels, allows manual recovery', async () => {
    seedStorage([TARGET, OTHER_PINNED, 'THIRD'])
    const { result } = renderHook(() => useDevice())
    api.wifiTunnelStartAndConnect.mockResolvedValueOnce(tunnelResult(OTHER_PINNED))
    await act(async () => { await result.current.startWifiTunnel('192.168.1.11', 50001) })
    const hostError = Object.assign(new Error('Windows Wintun unavailable (4319)'), { code: 'host_tunnel_unavailable' })
    api.wifiTunnelStartAndConnect.mockRejectedValue(hostError)
    act(() => {
      result.current.schedulePinReconnect(TARGET)
      result.current.schedulePinReconnect('THIRD', 10000)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(result.current.hostTunnelError).toBe(hostError.message)
    act(() => { result.current.schedulePinReconnect(TARGET) })
    await expect(result.current.autoStartWifiTunnel('192.168.1.10')).rejects.toBe(hostError)
    await act(async () => { await vi.advanceTimersByTimeAsync(300000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(2)
    expect(api.wifiTunnelDiscover).not.toHaveBeenCalled()
    expect(api.wifiTunnelStop).not.toHaveBeenCalled()
    expect(result.current.tunnels.map((tn) => tn.udid)).toEqual([OTHER_PINNED])
    expect(result.current.pinnedUdids).toEqual([TARGET, OTHER_PINNED, 'THIRD'])
    api.wifiTunnelStartAndConnect.mockResolvedValue(tunnelResult(TARGET))
    await act(async () => { await result.current.startWifiTunnel('192.168.1.10', 50000, TARGET) })
    expect(result.current.hostTunnelError).toBeNull()
    expect(result.current.tunnels).toHaveLength(2)
  })
})


describe('pin retry endpoint ownership', () => {
  test('skips all ports of healthy phones in saved and discovered candidates', async () => {
    seedStorage([TARGET, OTHER_PINNED])
    // The missing phone's saved IP has since been reassigned to a healthy phone.
    const healthy = { udid: OTHER_PINNED, ip: '192.168.1.10', port: 51000 }
    api.wifiTunnelStatus.mockResolvedValue({ tunnels: [healthy, { udid: 'SECOND-HEALTHY', ip: '192.168.1.30', port: 54000 }], running: true } as never)
    api.wifiTunnelDiscover.mockResolvedValue({ devices: [
      { ip: healthy.ip, port: 52000 },
      { ip: '192.168.1.30', port: 55000 },
      { ip: '192.168.1.20', port: 53000 },
    ] })
    api.wifiTunnelStartAndConnect.mockResolvedValue(tunnelResult(TARGET))
    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledTimes(1)
    expect(api.wifiTunnelStartAndConnect).toHaveBeenCalledWith('192.168.1.20', 53000, TARGET)
    expect(api.wifiTunnelStop).not.toHaveBeenCalled()
    expect(result.current.pinnedUdids).toEqual([TARGET, OTHER_PINNED])
  })

  test('does not probe when live ownership status is unavailable', async () => {
    seedStorage([TARGET])
    api.wifiTunnelStatus.mockRejectedValue(new Error('backend unavailable'))
    const { result } = renderHook(() => useDevice())
    act(() => { result.current.schedulePinReconnect(TARGET) })
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(api.wifiTunnelStatus).toHaveBeenCalledTimes(2)
    expect(api.wifiTunnelStartAndConnect).not.toHaveBeenCalled()
    expect(api.wifiTunnelDiscover).not.toHaveBeenCalled()
  })
})


test('protects phones recovering while discovery is pending, including websocket endpoint metadata', async () => {
  seedStorage([TARGET, OTHER_PINNED])
  localStorage.setItem('locwarp.tunnel.savedips', '[]')
  let finishDiscovery!: (value: { devices: Array<{ ip: string; port: number }> }) => void
  api.wifiTunnelDiscover.mockReturnValue(new Promise((resolve) => { finishDiscovery = resolve }))
  api.wifiTunnelStatus
    .mockResolvedValueOnce({ tunnels: [], running: false })
    .mockResolvedValue({ tunnels: [{ udid: 'RESTORED', ip: '192.168.1.30', port: 53000 }], running: true } as never)
  const listeners = new Set<(message: WsMessage) => void>()
  const subscribe = (listener: (message: WsMessage) => void) => {
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  }
  const { result } = renderHook(() => useDevice(subscribe))
  act(() => { result.current.schedulePinReconnect(TARGET) })
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(api.wifiTunnelDiscover).toHaveBeenCalledTimes(1)
  act(() => {
    for (const listener of listeners) listener({ type: 'tunnel_recovered', data: {
      udid: OTHER_PINNED, ip: '192.168.1.20', port: 52000, rsd_address: 'fd00::2', rsd_port: 1234,
    } } as WsMessage)
  })
  expect(result.current.tunnels[0]).toMatchObject({ ip: '192.168.1.20', port: 52000 })
  await act(async () => {
    finishDiscovery({ devices: [{ ip: '192.168.1.20', port: 52001 }, { ip: '192.168.1.30', port: 53001 }] })
  })
  expect(api.wifiTunnelStatus).toHaveBeenCalledTimes(2)
  expect(api.wifiTunnelStartAndConnect).not.toHaveBeenCalled()
  expect(api.wifiTunnelStop).not.toHaveBeenCalled()
})
