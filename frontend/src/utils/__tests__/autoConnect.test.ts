import { describe, expect, test } from 'vitest'
import { buildAutoConnectCandidates, classifyPinAttempt, pinnedUdidsNeedingRetry } from '../autoConnect'

const noTunnels = new Set<string>()

describe('buildAutoConnectCandidates', () => {
  test('with pins: merges saved and discovered endpoints by IP', () => {
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
      {
        ip: '192.168.1.10',
        port: 61234,
        ports: [61234, 50000],
        udid: 'PINNED',
      },
    ])
  })

  test('without pins: discovery-backed groups outrank saved-only groups', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.11', port: 50001, udid: 'ANY' }],
      discovered: [{ ip: '192.168.1.12', port: 50002 }],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
    })
    expect(result.map((c) => c.ip)).toEqual(['192.168.1.12', '192.168.1.11'])
  })

  test('excludes an IP when any merged endpoint is already tunneled', () => {
    const result = buildAutoConnectCandidates({
      saved: [{ ip: '192.168.1.10', port: 50000, udid: 'PINNED' }],
      discovered: [
        { ip: '192.168.1.10', port: 61234 },   // merged with saved IP
        { ip: '192.168.1.13', port: 50003 },
      ],
      pinnedUdids: ['PINNED'],
      alreadyTunneled: new Set(['192.168.1.10:61234']),
    })
    expect(result).toEqual([{ ip: '192.168.1.13', port: 50003, udid: undefined }])
  })

  test('stale saved A plus fresh discovered A/B/C still fills max with live IPs', () => {
    const result = buildAutoConnectCandidates({
      saved: [
        { ip: '192.168.1.10', port: 41000, udid: 'PHONE-A' },
        { ip: '192.168.1.99', port: 41999, udid: 'STALE-SAVED' },
      ],
      discovered: [
        { ip: '192.168.1.10', port: 51000, ports: [51000, 51001] },
        { ip: '192.168.1.11', port: 52000 },
        { ip: '192.168.1.12', port: 53000 },
      ],
      pinnedUdids: [],
      alreadyTunneled: noTunnels,
      max: 3,
    })

    expect(result.map((candidate) => candidate.ip)).toEqual([
      '192.168.1.10',
      '192.168.1.11',
      '192.168.1.12',
    ])
    expect(result[0]).toEqual({
      ip: '192.168.1.10',
      port: 51000,
      ports: [51000, 51001, 41000],
      udid: 'PHONE-A',
    })
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
