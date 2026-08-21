// @vitest-environment happy-dom

import { afterEach, describe, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { I18nProvider } from '../../i18n'
import PhoneControlButton, { buildPhoneControlUrl, type PhoneInfo } from '../PhoneControl'

const OLD_TOKEN = '0123456789abcdef0123456789abcdef'
const NEW_TOKEN = 'fedcba9876543210fedcba9876543210'
const IP = '192.168.50.10'
const SECOND_IP = '192.168.50.11'

const info = (token: string, lanIps: string[] = [IP]): PhoneInfo => ({
  port: 8777,
  lan_ips: lanIps,
  token,
})

const jsonResponse = (body: unknown, status = 200): Response => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
} as Response)

const renderPhoneControl = (showToast: (msg: string) => void) => render(
  <I18nProvider>
    <PhoneControlButton showToast={showToast} />
  </I18nProvider>,
)

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('buildPhoneControlUrl', () => {
  test('embeds the capability token in the URL fragment', () => {
    expect(
      buildPhoneControlUrl(
        { port: 8777, token: '0123456789abcdef0123456789abcdef' },
        '192.168.50.10',
      ),
    ).toBe('http://192.168.50.10:8777/phone#t=0123456789abcdef0123456789abcdef')
  })

  test('returns blank when any required URL component is missing', () => {
    expect(buildPhoneControlUrl(null, '192.168.50.10')).toBe('')
    expect(buildPhoneControlUrl({ port: 8777, token: '' }, '192.168.50.10')).toBe('')
    expect(buildPhoneControlUrl({ port: 8777, token: 'token' }, '')).toBe('')
    expect(buildPhoneControlUrl({ port: 0, token: 'token' }, '192.168.50.10')).toBe('')
  })
})

describe('PhoneControl rotate lifecycle', () => {
  test('does not report rotate success when the follow-up info request fails', async () => {
    const showToast = vi.fn()
    let rotated = false
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/phone/rotate')) {
        rotated = true
        return Promise.resolve(jsonResponse({ status: 'ok' }))
      }
      if (url.endsWith('/api/phone/info')) {
        return rotated
          ? Promise.resolve(jsonResponse({ detail: 'backend unavailable' }, 503))
          : Promise.resolve(jsonResponse(info(OLD_TOKEN)))
      }
      return Promise.resolve(jsonResponse({}, 404))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPhoneControl(showToast)
    fireEvent.click(screen.getByRole('button', { name: /phone control|手機操控/i }))
    await waitFor(() => expect(screen.getByText(buildPhoneControlUrl(info(OLD_TOKEN), IP))).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /regenerate access url|重新產生操控網址/i }))
    await waitFor(() => expect(screen.getByText('HTTP 503')).toBeTruthy())

    expect(showToast).not.toHaveBeenCalled()
    expect(screen.queryByText(buildPhoneControlUrl(info(OLD_TOKEN), IP))).toBeNull()
    expect(screen.queryByTitle(/copy the complete control URL|複製完整操控網址/i)).toBeNull()
  })

  test('does not let an older in-flight info response overwrite the rotated URL', async () => {
    const showToast = vi.fn()
    let infoCalls = 0
    let resolveStaleInfo: ((response: Response) => void) | undefined
    const staleInfo = new Promise<Response>((resolve) => { resolveStaleInfo = resolve })
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/phone/rotate')) {
        return Promise.resolve(jsonResponse({ status: 'ok' }))
      }
      if (url.endsWith('/api/phone/info')) {
        infoCalls += 1
        if (infoCalls === 1) return Promise.resolve(jsonResponse(info(OLD_TOKEN, [IP, SECOND_IP])))
        if (infoCalls === 2) return staleInfo
        if (infoCalls === 3) return Promise.resolve(jsonResponse(info(OLD_TOKEN, [IP, SECOND_IP])))
        return Promise.resolve(jsonResponse(info(NEW_TOKEN, [IP, SECOND_IP])))
      }
      return Promise.resolve(jsonResponse({}, 404))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPhoneControl(showToast)
    fireEvent.click(screen.getByRole('button', { name: /phone control|手機操控/i }))
    await waitFor(() => expect(screen.getByText(buildPhoneControlUrl(info(OLD_TOKEN), IP))).toBeTruthy())
    await waitFor(() => expect(infoCalls).toBeGreaterThanOrEqual(2))

    // Change the displayed NIC to trigger another info refresh.  That
    // request completes and re-enables Rotate while the older request above
    // is still pending, making the race deterministic without timers.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: SECOND_IP } })
    await waitFor(() => expect(infoCalls).toBeGreaterThanOrEqual(3))
    const rotateButton = screen.getByRole('button', { name: /regenerate access url|重新產生操控網址/i })
    await waitFor(() => expect((rotateButton as HTMLButtonElement).disabled).toBe(false))
    await act(async () => { fireEvent.click(rotateButton) })
    await waitFor(() => expect(screen.getByText(buildPhoneControlUrl(info(NEW_TOKEN, [IP, SECOND_IP]), SECOND_IP))).toBeTruthy())

    await act(async () => {
      resolveStaleInfo?.(jsonResponse(info(OLD_TOKEN)))
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(screen.getByText(buildPhoneControlUrl(info(NEW_TOKEN, [IP, SECOND_IP]), SECOND_IP))).toBeTruthy()
      expect(screen.queryByText(buildPhoneControlUrl(info(OLD_TOKEN, [IP, SECOND_IP]), SECOND_IP))).toBeNull()
    })
  })
})
