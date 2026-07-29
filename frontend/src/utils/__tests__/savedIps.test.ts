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
})
