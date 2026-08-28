// @vitest-environment happy-dom

import { afterEach, describe, expect, test } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { I18nProvider } from '../../i18n'
import { DeviceChip } from '../DeviceChip'
import type { DeviceInfo } from '../../hooks/useDevice'
import type { DeviceRuntime } from '../../hooks/useSimulation'

const device: DeviceInfo = {
  udid: 'TEST-UDID',
  name: 'Test iPhone',
  ios_version: '26.6',
  connection_type: 'Network',
  is_connected: true,
}

const staleRuntime: DeviceRuntime = {
  udid: device.udid,
  state: 'disconnected',
  currentPos: null,
  destination: null,
  routePath: [],
  progress: 0,
  eta: 0,
  distanceRemaining: 0,
  distanceTraveled: 0,
  waypointIndex: null,
  currentSpeedKmh: 0,
  error: null,
  lapCount: 0,
  cooldown: 0,
}

afterEach(() => cleanup())

describe('DeviceChip', () => {
  test('shows connected when backend says the device recovered but runtime is stale', () => {
    render(
      <I18nProvider>
        <DeviceChip
          letter="A"
          device={device}
          runtime={staleRuntime}
          onDisconnect={() => {}}
          onRestoreOne={() => {}}
        />
      </I18nProvider>,
    )

    expect(screen.getByText(/Connected|已連線/)).toBeTruthy()
    expect(screen.queryByText(/Disconnected|已斷線/)).toBeNull()
  })
})
