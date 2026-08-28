// @vitest-environment happy-dom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getStatus: vi.fn(),
}))

vi.mock('../../services/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/api')>()),
  getStatus: api.getStatus,
}))

import { useSimulation, type WsSubscribe } from '../useSimulation'
import type { WsMessage } from '../useWebSocket'

const PAULINE = 'pauline-primary'

const makeStorage = (): Storage => {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    key: (index: number) => [...values.keys()][index] ?? null,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, String(value)) },
    removeItem: (key: string) => { values.delete(key) },
    clear: () => { values.clear() },
  }
}

beforeEach(() => {
  vi.stubGlobal('localStorage', makeStorage())
  api.getStatus.mockResolvedValue({
    state: 'idle',
    current_position: null,
    speed_mps: 0,
    is_paused: false,
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('authoritative simulation state', () => {
  test('hydrates the backend status response shape on mount', async () => {
    api.getStatus.mockResolvedValue({
      state: 'random_walk',
      current_position: { lat: 24.829, lng: 121.011 },
      speed_mps: 3,
      is_paused: false,
    })

    const { result } = renderHook(() => useSimulation(undefined, PAULINE))

    await waitFor(() => {
      expect(result.current.status).toMatchObject({
        running: true,
        paused: false,
        state: 'random_walk',
        speed: 3,
      })
      expect(result.current.currentPosition).toEqual({ lat: 24.829, lng: 121.011 })
    })
  })

  test('stops the UI when the primary disconnects while another device remains', async () => {
    let listener: ((message: WsMessage) => void) | undefined
    const subscribe: WsSubscribe = (fn) => {
      listener = fn
      return () => { listener = undefined }
    }
    const { result } = renderHook(() => useSimulation(subscribe, PAULINE))
    await waitFor(() => expect(api.getStatus).toHaveBeenCalled())

    act(() => {
      listener?.({ type: 'state_change', data: { udid: PAULINE, state: 'random_walk' } })
    })
    expect(result.current.status.running).toBe(true)

    act(() => {
      listener?.({
        type: 'device_disconnected',
        data: { udid: PAULINE, remaining_count: 1 },
      })
    })

    expect(result.current.status).toMatchObject({ running: false, paused: false })
  })
})
