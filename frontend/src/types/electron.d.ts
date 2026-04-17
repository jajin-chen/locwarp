export interface LocatePcResult {
  ok: boolean
  lat?: number
  lng?: number
  accuracy?: number
  code?: 'DENIED' | 'TIMEOUT' | 'UNKNOWN' | 'ERROR' | 'SPAWN_FAILED'
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
