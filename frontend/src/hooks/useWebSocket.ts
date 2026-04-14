import { useState, useEffect, useRef, useCallback } from 'react'

export interface WsMessage {
  type: string
  data: any
}

const WS_URL = 'ws://127.0.0.1:8777/ws/status'
const RECONNECT_INTERVAL = 3000
const MAX_RECONNECT_INTERVAL = 30000

/**
 * WebSocket hook using a subscribe-callback pattern for message delivery.
 *
 * **Why not useState<WsMessage>?** The previous implementation stored each
 * incoming message in a single `lastMessage` useState and let consumers
 * react via `useEffect(..., [lastMessage])`. When two messages arrived in
 * the same microtask (e.g. a stop+route_path pair during mode-switch),
 * React 18 auto-batching coalesced the setStates: the intermediate message
 * was overwritten before the effect fired, so its branch never ran. That
 * dropped events like `state_change(idle)` and left stale route polylines
 * on the map (see issue #5). Subscriber callbacks run synchronously on
 * every onmessage, so no batching can drop a message.
 */
export function useWebSocket() {
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const subscribersRef = useRef<Set<(m: WsMessage) => void>>(new Set())
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectDelay = useRef(RECONNECT_INTERVAL)
  const mountedRef = useRef(true)

  const cleanup = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }
  }, [])

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return

    try {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        if (!mountedRef.current) {
          ws.close()
          return
        }
        setConnected(true)
        reconnectDelay.current = RECONNECT_INTERVAL
      }

      ws.onmessage = (event) => {
        if (!mountedRef.current) return
        try {
          const msg: WsMessage = JSON.parse(event.data)
          // Fan out synchronously: no state, no batching, no drops.
          subscribersRef.current.forEach((fn) => {
            try { fn(msg) } catch { /* subscriber errors shouldn't kill the stream */ }
          })
        } catch {
          // ignore malformed messages
        }
      }

      ws.onclose = () => {
        if (!mountedRef.current) return
        setConnected(false)
        wsRef.current = null
        scheduleReconnect()
      }

      ws.onerror = () => {
        ws.close()
      }
    } catch {
      scheduleReconnect()
    }
  }, [])

  const scheduleReconnect = useCallback(() => {
    cleanup()
    if (!mountedRef.current) return
    reconnectTimer.current = setTimeout(() => {
      reconnectDelay.current = Math.min(
        reconnectDelay.current * 1.5,
        MAX_RECONNECT_INTERVAL,
      )
      connect()
    }, reconnectDelay.current)
  }, [connect, cleanup])

  const sendMessage = useCallback((type: string, data: any = {}) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type, data }))
    }
  }, [])

  /**
   * Subscribe to every incoming WebSocket message. Returns an unsubscribe
   * function. Safe to call from useEffect — stable identity across renders.
   */
  const subscribe = useCallback((fn: (m: WsMessage) => void) => {
    subscribersRef.current.add(fn)
    return () => { subscribersRef.current.delete(fn) }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      cleanup()
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
      setConnected(false)
    }
  }, [connect, cleanup])

  return { connected, subscribe, sendMessage }
}
