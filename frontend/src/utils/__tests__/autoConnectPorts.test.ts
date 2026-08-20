import { describe, expect, test } from 'vitest'
import { buildAutoConnectCandidates } from '../autoConnect'

describe('buildAutoConnectCandidates RemotePairing port hints', () => {
  test('preserves saved port hints for the backend fallback loop', () => {
    const result = buildAutoConnectCandidates({
      saved: [{
        ip: '192.0.2.10',
        port: 51234,
        ports: [51234, 52000],
        udid: 'PHONE-A',
      }],
      discovered: [],
      pinnedUdids: [],
      alreadyTunneled: new Set(),
    })

    expect(result).toEqual([{
      ip: '192.0.2.10',
      port: 51234,
      ports: [51234, 52000],
      udid: 'PHONE-A',
    }])
  })

  test('groups multiple scanned ports for one IP so max counts devices, not ports', () => {
    const result = buildAutoConnectCandidates({
      saved: [],
      discovered: [
        { ip: '192.0.2.10', port: 51234, ports: [51234, 52000] },
        { ip: '192.0.2.10', port: 52000, ports: [52000, 53000] },
        { ip: '192.0.2.10', port: 53000, ports: [53000] },
        { ip: '192.0.2.11', port: 54000, ports: [54000] },
      ],
      pinnedUdids: [],
      alreadyTunneled: new Set(),
      max: 2,
    })

    expect(result).toEqual([
      {
        ip: '192.0.2.10',
        port: 51234,
        ports: [51234, 52000, 53000],
        udid: undefined,
      },
      {
        ip: '192.0.2.11',
        port: 54000,
        ports: [54000],
        udid: undefined,
      },
    ])
  })

  test('keeps a manual primary port first and de-dupes DeviceStatus-style hints', () => {
    const result = buildAutoConnectCandidates({
      saved: [{
        ip: '192.0.2.10',
        port: 51234,
        ports: [52000, 51234, 52000, 53000],
        udid: 'PHONE-A',
      }],
      discovered: [],
      pinnedUdids: [],
      alreadyTunneled: new Set(),
    })

    expect(result).toEqual([{
      ip: '192.0.2.10',
      port: 51234,
      ports: [51234, 52000, 53000],
      udid: 'PHONE-A',
    }])
  })
})
