export interface LocatePcResult {
  ok: boolean
  lat?: number
  lng?: number
  accuracy?: number
  via?: 'windows' | 'ipwho.is' | 'ipapi.co' | 'freeipapi.com'
  code?: 'DENIED' | 'TIMEOUT' | 'UNKNOWN' | 'ERROR' | 'SPAWN_FAILED' | 'NODATA' | 'ALL_FAILED'
  message?: string
}

declare global {
  interface Window {
    electronAPI?: {
      locatePc(): Promise<LocatePcResult>
    }
  }
}

export {}
