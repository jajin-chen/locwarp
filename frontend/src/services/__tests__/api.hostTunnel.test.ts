import { afterEach, expect, test, vi } from 'vitest'
import { wifiTunnelStartAndConnect } from '../api'

afterEach(() => vi.unstubAllGlobals())

test('preserves host failure code and actionable message without HTTP retries', async () => {
  const fetch = vi.fn().mockResolvedValue({
    ok: false,
    statusText: 'Service Unavailable',
    json: async () => ({ detail: { code: 'host_tunnel_unavailable', message: 'Repair Windows adapter (4319)', retryable: false } }),
  })
  vi.stubGlobal('fetch', fetch)
  await expect(wifiTunnelStartAndConnect('192.168.1.10')).rejects.toMatchObject({
    code: 'host_tunnel_unavailable', message: 'Repair Windows adapter (4319)',
  })
  expect(fetch).toHaveBeenCalledTimes(1)
})
