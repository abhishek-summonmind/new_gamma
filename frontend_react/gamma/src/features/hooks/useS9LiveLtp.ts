import { useEffect, useState } from 'react'

export type S9LiveLtp = {
  ltp: number
  tsInMillis: number | null
}

const identityKey = (symbol: unknown, expiry: unknown, strike: unknown, optionType: unknown) =>
  [String(symbol || '').toUpperCase(), String(expiry || ''), Number(strike), String(optionType || '').toUpperCase()].join('|')

const websocketUrl = () => {
  const configured = import.meta.env.VITE_API_BASE_URL || window.location.origin
  const base = new URL(configured, window.location.origin)
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  base.pathname = `${base.pathname.replace(/\/$/, '')}/ws/v1/s9/ltp`
  base.search = ''
  base.hash = ''
  return base.toString()
}

export const s9LiveLtpKey = identityKey

export const useS9LiveLtp = (cacheVersion?: unknown) => {
  const [prices, setPrices] = useState<Record<string, S9LiveLtp>>({})

  useEffect(() => {
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let stopped = false

    const connect = () => {
      if (stopped) return
      socket = new WebSocket(websocketUrl())
      socket.onmessage = (event) => {
        let update: any
        try {
          update = JSON.parse(event.data)
        } catch {
          return
        }
        if (update?.type !== 's9_ltp' || !Number.isFinite(Number(update?.ltp))) return
        const key = identityKey(update.symbol, update.expiry, update.strike, update.option_type)
        setPrices((current) => ({
          ...current,
          [key]: {
            ltp: Number(update.ltp),
            tsInMillis: Number.isFinite(Number(update.ts_in_millis)) ? Number(update.ts_in_millis) : null,
          },
        }))
      }
      socket.onclose = () => {
        if (!stopped) reconnectTimer = window.setTimeout(connect, 2_000)
      }
    }

    setPrices({})
    connect()
    return () => {
      stopped = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [cacheVersion])

  return prices
}
