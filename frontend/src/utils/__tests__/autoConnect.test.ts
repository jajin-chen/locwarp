import { describe, expect, test } from 'vitest'
import { buildAutoConnectCandidates } from '../autoConnect'

const noTunnels = new Set<string>()

describe('buildAutoConnectCandidates', () => {
  test('with pins: pinned saved entries first, then discovered endpoints', () => {
    const result = buildAutoConnectCandidates({
      saved: [
        { ip: '192.168.1.10', port: 50000, udid: 'PINNED' },
        { ip: '192.168.1.11', port: 50001, udid: 'OTHER' },
      ],
      discovered: [{ ip: '192.168.1.10', port: 61234 }],
      pinnedUdids: ['PINNED'],
      alreadyTunneled: noTunnels,
    })
    expect(result).toEqual([
      { ip: '192.168.1.10', port: 50000, udid: 'PINNED' },
      { ip: '192.168.1.10', port: 61234, udid: undefined },
    ])
  })

  test('without pins: keeps all saved entries plus discovered', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.11', port: 50001, udid: 'ANY' }],
      discovered: [{ ip: '192.168.1.12', port: 50002 }],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
    })
    expect(result.map((c) => c.ip)).toEqual(['192.168.1.11', '192.168.1.12'])
  })

  test('excludes endpoints already tunneled and dedups ip:port', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.10', port: 50000, udid: 'PINNED' }],
      discovered: [
        { ip: '192.168.1.10', port: 50000 },   // dup of saved
        { ip: '192.168.1.13', port: 50003 },   // already tunneled
      ],
      pinnedUdids: ['PINNED'],
      alreadyTunneled: new Set(['192.168.1.13:50003']),
    })
    expect(result).toEqual([{ ip: '192.168.1.10', port: 50000, udid: 'PINNED' }])
  })

  test('with pins: matches saved entry udid case-insensitively', () => {
    const result = buildAutoConnectCandidates({
      saved: [
        { ip: '192.168.1.10', port: 50000, udid: 'abcd1234' },
        { ip: '192.168.1.11', port: 50001, udid: 'OTHER' },
      ],
      discovered: [],
      pinnedUdids: ['ABCD1234'],
      alreadyTunneled: noTunnels,
    })
    expect(result).toEqual([
      { ip: '192.168.1.10', port: 50000, udid: 'abcd1234' },
    ])
  })

  test('caps at max (default 3)', () => {
    const result = buildAutoConnectCandidates({
      saved: [],
      discovered: [
        { ip: '192.168.1.20', port: 1 }, { ip: '192.168.1.21', port: 2 },
        { ip: '192.168.1.22', port: 3 }, { ip: '192.168.1.23', port: 4 },
      ],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
    })
    expect(result).toHaveLength(3)
  })
})
