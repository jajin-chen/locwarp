import { wifiHostNetwork } from '../services/api'

const CACHE_MS = 10_000
let cachedPrefix: string | null = null
let cacheExpiresAt = 0
let requestInFlight: Promise<string | null> | null = null

// `/24` matches the existing tunnel-discovery heuristic. It is a hint only:
// when Windows cannot identify a useful default-route address, callers keep
// saved candidates eligible instead of filtering them out.
export function getHostNetworkPrefix(): Promise<string | null> {
  if (Date.now() < cacheExpiresAt) return Promise.resolve(cachedPrefix)
  if (requestInFlight) return requestInFlight

  requestInFlight = wifiHostNetwork()
    .then(({ prefix }) => typeof prefix === 'string' && prefix ? prefix : null)
    .catch(() => null)
    .then((prefix) => {
      cachedPrefix = prefix
      cacheExpiresAt = Date.now() + CACHE_MS
      return prefix
    })
    .finally(() => { requestInFlight = null })

  return requestInFlight
}
