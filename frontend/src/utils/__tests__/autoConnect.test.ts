import { describe, expect, test } from 'vitest'
import { buildAutoConnectCandidates, classifyPinAttempt, pinnedUdidsNeedingRetry } from '../autoConnect'

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

describe('pinnedUdidsNeedingRetry', () => {
  test('returns pinned udids that have no live tunnel', () => {
    const result = pinnedUdidsNeedingRetry({
      pinnedUdids: ['AAAA', 'BBBB', 'CCCC'],
      liveTunnelUdids: ['BBBB'],
    })
    expect(result).toEqual(['AAAA', 'CCCC'])
  })

  test('matches live tunnel udids case-insensitively', () => {
    const result = pinnedUdidsNeedingRetry({
      pinnedUdids: ['AbCd1234'],
      liveTunnelUdids: ['abcd1234'],
    })
    expect(result).toEqual([])
  })

  test('returns empty array when nothing is pinned', () => {
    const result = pinnedUdidsNeedingRetry({
      pinnedUdids: [],
      liveTunnelUdids: ['abcd1234'],
    })
    expect(result).toEqual([])
  })
})

describe('classifyPinAttempt', () => {
  test('reconnected: resolved udid matches the target', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: 'TARGET',
      pinnedUdids: ['TARGET'],
    })
    expect(result).toBe('reconnected')
  })

  test('reconnected: matches case-insensitively', () => {
    const result = classifyPinAttempt({
      targetUdid: 'AbCd1234',
      resultUdid: 'abcd1234',
      pinnedUdids: ['AbCd1234'],
    })
    expect(result).toBe('reconnected')
  })

  test('other-pinned: resolved udid belongs to a different pinned device', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: 'OTHER',
      pinnedUdids: ['TARGET', 'OTHER'],
    })
    expect(result).toBe('other-pinned')
  })

  test('other-pinned: matches pinned list case-insensitively', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: 'oThEr',
      pinnedUdids: ['TARGET', 'OTHER'],
    })
    expect(result).toBe('other-pinned')
  })

  test('stranger: resolved udid is not in the pinned list', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: 'RANDOM',
      pinnedUdids: ['TARGET'],
    })
    expect(result).toBe('stranger')
  })

  test('failed: no resultUdid (undefined)', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: undefined,
      pinnedUdids: ['TARGET'],
    })
    expect(result).toBe('failed')
  })

  test('failed: resultUdid is null', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: null,
      pinnedUdids: ['TARGET'],
    })
    expect(result).toBe('failed')
  })

  test('failed: resultUdid is an empty string', () => {
    const result = classifyPinAttempt({
      targetUdid: 'TARGET',
      resultUdid: '',
      pinnedUdids: ['TARGET'],
    })
    expect(result).toBe('failed')
  })
})
