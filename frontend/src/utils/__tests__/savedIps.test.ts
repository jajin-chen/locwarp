import { describe, expect, test } from 'vitest'
import { removeSavedIpByUdid, upsertSavedIp, type SavedIpEntry } from '../savedIps'

const entry = (over: Partial<SavedIpEntry>): SavedIpEntry => ({
  ip: '192.168.1.10', port: 50000, udid: 'U1', name: 'iPhone', lastUsed: 100, ...over,
})

describe('upsertSavedIp', () => {
  test('prepends new entry and dedups by udid (device moved to new endpoint)', () => {
    const list = [entry({ ip: '192.168.1.10', port: 50000, udid: 'U1' })]
    const next = upsertSavedIp(list, entry({ ip: '192.168.1.10', port: 51111, udid: 'U1', lastUsed: 200 }))
    expect(next).toHaveLength(1)
    expect(next[0].port).toBe(51111)
  })

  test('dedups by (ip, port) when udid differs', () => {
    const list = [entry({ udid: 'U1' })]
    const next = upsertSavedIp(list, entry({ udid: 'U2', lastUsed: 200 }))
    expect(next).toHaveLength(1)
    expect(next[0].udid).toBe('U2')
  })

  test('caps the ring buffer at 5 entries', () => {
    let list: SavedIpEntry[] = []
    for (let i = 0; i < 6; i++) {
      list = upsertSavedIp(list, entry({ ip: `192.168.1.${10 + i}`, udid: `U${i}`, lastUsed: i }))
    }
    expect(list).toHaveLength(5)
    expect(list[0].udid).toBe('U5')
  })

  test('does not mutate the input list', () => {
    const list = [entry({})]
    const snapshot = JSON.parse(JSON.stringify(list))
    upsertSavedIp(list, entry({ udid: 'U9', ip: '192.168.1.99' }))
    expect(list).toEqual(snapshot)
  })
})

describe('removeSavedIpByUdid', () => {
  test('removes matching udid, keeps others, does not mutate', () => {
    const list = [entry({ udid: 'U1' }), entry({ udid: 'U2', ip: '192.168.1.11' })]
    const next = removeSavedIpByUdid(list, 'U1')
    expect(next.map((e) => e.udid)).toEqual(['U2'])
    expect(list).toHaveLength(2)
  })

  test('removes matching udid case-insensitively', () => {
    // Regression guard: savedips entries are written with the udid casing
    // reported by RSD at tunnel-connect time, while a caller scrubbing a
    // stale entry (e.g. the pin-retry loop) may be chasing a udid sourced
    // from usbmuxd's device list, which can differ in case for the same
    // physical device.
    const list = [entry({ udid: 'AbCd1234' }), entry({ udid: 'U2', ip: '192.168.1.11' })]
    const next = removeSavedIpByUdid(list, 'ABCD1234')
    expect(next.map((e) => e.udid)).toEqual(['U2'])
  })

  test('equivalent to a udid lookup returning null after removal (readSavedEntryFor semantics)', () => {
    // useDevice.ts's readSavedEntryFor does `arr.find(e => e.udid.toLowerCase()
    // === udidLc)` over this same saved-ips shape. Once a target udid's own
    // stale entry is scrubbed via removeSavedIpByUdid, that lookup must miss
    // so the pin-retry loop falls through to rediscovering the device fresh
    // instead of redialing the same dead endpoint.
    const list = [entry({ udid: 'TARGET', ip: '192.168.1.50', port: 49152 }), entry({ udid: 'OTHER', ip: '192.168.1.11' })]
    const next = removeSavedIpByUdid(list, 'TARGET')
    const hit = next.find((e) => typeof e.udid === 'string' && e.udid.toLowerCase() === 'target')
    expect(hit).toBeUndefined()
  })
})
