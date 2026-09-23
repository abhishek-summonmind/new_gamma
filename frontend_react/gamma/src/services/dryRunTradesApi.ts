import { api } from './client'

export type DryRunTrade = {
  id: number
  instrument: string
  strike: number | null
  type: string
  entry_time: string | null
  entry_price: number | null
  entry_score: number | null
  entry_pass: string | null
  ltp: number | null
  sl: number | null
  status: string
  exit_time: string | null
  exit_price: number | null
  exit_reason: string | null
}

export type DryRunTradesResponse = {
  count: number
  items: DryRunTrade[]
}

export const getDryRunTrades = async ({ signal }: { signal?: AbortSignal } = {}) => {
  const response = await api.get<DryRunTradesResponse>('/trades/dry-run', {
    params: { limit: 100 },
    signal,
    timeout: 12_000,
  })
  return response.data
}
