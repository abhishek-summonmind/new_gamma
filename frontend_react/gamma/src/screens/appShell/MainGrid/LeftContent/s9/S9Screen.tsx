import { useMemo } from 'react'

import { Loader2, Trophy } from 'lucide-react'

import { useS9 } from '../../../../../features/hooks/useS9'

const optionTypeLabel = (value: any) => {
  const type = String(value || '').toUpperCase()
  if (type === 'CE') return 'Call'
  if (type === 'PE') return 'Put'
  return '-'
}

const formatExpiry = (value: any) => {
  if (!value) return '-'
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return match ? `${match[2]}-${match[3]}` : String(value)
}

const displayValue = (value: any, digits?: number) => {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number' && Number.isFinite(value)) {
    return digits === undefined ? String(value) : value.toFixed(digits)
  }
  return String(value)
}

const formatStrike = (value: any, optionType: any) => {
  if (value === null || value === undefined || value === '') return '-'
  const numeric = Number(value)
  const strike = Number.isFinite(numeric)
    ? numeric.toLocaleString('en-IN')
    : String(value)
  const type = String(optionType || '').toUpperCase()
  return type ? `${strike} ${type}` : strike
}

const filterColumns = [
  { key: 'sweep', label: 'Sweep / EMA9' },
  { key: 'stoch_rsi', label: 'Stoch RSI' },
  { key: 'supertrend', label: 'Super Trend' },
  { key: 'delta', label: 'Delta' },
  { key: 'pcr', label: 'PCR' },
  { key: 'vwap', label: 'VWAP' },
  { key: 'order_book', label: 'Order Book' },
  { key: 'volume_breakout', label: '3/5 Vol' },
]

const filterStatus = (payload: any, key: string): 'pass' | 'fail' | 'na' => {
  const stored = payload?.stored_filter_row || null
  const storedValue = stored
    ? key === 'sweep'
      ? stored.sweep ?? stored.ema9
      : stored[key]
    : undefined
  if (storedValue === true) return 'pass'
  if (storedValue === false) return 'fail'

  const filters = payload?.filters && typeof payload.filters === 'object'
    ? payload.filters
    : {}
  const filter = filters[key] || (key === 'sweep' ? filters.ema9 : null)
  if (!filter || typeof filter !== 'object' || filter.data?.ignored) return 'na'
  return filter.passed ? 'pass' : 'fail'
}

const formatScore = (payload: any, item: any) => {
  const rawScore = Number(payload?.score ?? item?.confidence ?? 0)
  const maxScore = Number(payload?.max_score || 100)
  const score = rawScore <= 1 && maxScore === 100 ? rawScore * 100 : rawScore
  return `${displayValue(score, 0)}/${displayValue(maxScore, 0)}`
}

const formatMove = (ltp: any, gtp: any) => {
  const current = Number(ltp)
  const generated = Number(gtp)
  if (!Number.isFinite(current) || !Number.isFinite(generated)) return '-'
  const move = current - generated
  return `${move > 0 ? '+' : ''}${move.toFixed(2)}`
}

const formatTime = (value: any) => {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return date.toLocaleTimeString('en-IN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

const getSelectedStrikeLtp = (payload: any, strike: any, optionType: any) => {
  const strikes = Array.isArray(payload?.scanned_strikes)
    ? payload.scanned_strikes
    : []
  const selected = strikes.find(
    (item: any) =>
      Number(item?.strike) === Number(strike) &&
      String(item?.option_type || '').toUpperCase() === String(optionType || '').toUpperCase(),
  )
  return selected?.ltp ?? payload?.ltp ?? null
}

const formatRsi = (payload: any) => {
  const rsi = payload?.underlying_rsi
  const numeric = Number(rsi)
  return {
    value: rsi === null || rsi === undefined || rsi === '' || !Number.isFinite(numeric)
      ? '-'
      : displayValue(numeric, 2),
    timeframe: payload?.underlying_rsi_timeframe
      ? String(payload.underlying_rsi_timeframe)
      : '',
  }
}

const premiumLevels = (payload: any) => {
  const levels = payload?.premium_swing_levels || payload?.swing_levels || {}
  const previous = levels?.previous_15m || {}
  const high = payload?.premium_high ?? previous?.high ?? levels?.resistance
  const low = payload?.premium_low ?? previous?.low ?? levels?.support
  return {
    high,
    low,
    support: payload?.premium_support ?? levels?.support ?? low,
    resistance: payload?.premium_resistance ?? levels?.resistance ?? high,
  }
}

const FilterBadge = ({ status }: { status: 'pass' | 'fail' | 'na' }) => (
  <span
    className={`inline-flex min-w-[24px] justify-center rounded-full border px-0.5 py-0.5 text-[0.45rem] font-bold uppercase ${
      status === 'pass'
        ? 'border-[#ABEFC6] bg-[#ECFDF3] text-[#027A48]'
        : status === 'fail'
          ? 'border-[#FECDCA] bg-[#FEF3F2] text-[#B42318]'
          : 'border-gray-200 bg-gray-50 text-gray-500'
    }`}
  >
    {status === 'pass' ? 'Pass' : status === 'fail' ? 'Fail' : '-'}
  </span>
)

const TradeStatus = ({ value }: { value: string }) => {
  const status = value || 'UNKNOWN'
  const colors = status.startsWith('IN')
    ? 'border-[#ABEFC6] bg-[#ECFDF3] text-[#027A48]'
    : status === 'PENDING'
      ? 'border-[#FEDF89] bg-[#FFFAEB] text-[#B54708]'
    : status.startsWith('OUT')
        ? 'border-gray-300 bg-gray-100 text-gray-700'
        : 'border-gray-200 bg-white text-gray-500'
  return <span className={`inline-block rounded-full border px-1.5 py-1 text-[0.52rem] font-bold ${colors}`}>{status}</span>
}

function S9Screen() {
  const { data, error, isLoading, isFetching, refetch } = useS9()
  const opportunities = useMemo(() => {
    const items = Array.isArray(data?.items) ? data.items : []
    const selected: any[] = []
    const seen = new Set<string>()
    for (const item of items) {
      const symbol = String(item?.payload?.underlying_symbol || item?.symbol || '').toUpperCase()
      if (!symbol || seen.has(symbol)) continue
      selected.push(item)
      seen.add(symbol)
      if (selected.length === 6) break
    }
    for (const symbol of ['NIFTY 50', 'SENSEX']) {
      if (seen.has(symbol)) continue
      if (selected.length === 6) {
        for (let index = selected.length - 1; index >= 0; index -= 1) {
          const value = String(selected[index]?.payload?.underlying_symbol || selected[index]?.symbol || '').toUpperCase()
          if (value !== 'NIFTY 50' && value !== 'SENSEX') {
            selected.splice(index, 1)
            break
          }
        }
      }
      selected.push({
        symbol,
        confidence: 0,
        payload: { underlying_symbol: symbol, score: 0, passed_count: 0, total_filters: 0, data_pending: true },
      })
      seen.add(symbol)
    }
    return selected.slice(0, 6)
  }, [data])
  const tableColumnCount = 19 + filterColumns.length

  return (
    <article className="relative w-full min-w-0 overflow-hidden bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Trophy className="h-5 w-5 shrink-0 text-[#175CD3]" />
            <h1 className="text-lg font-bold text-gray-950">Top Opportunities</h1>
          </div>
          <p className="mt-1 text-xs font-medium text-gray-600">
            Market-wide S9 ranking by filter pass score. Current screen selection is ignored.
          </p>
        </div>
        <div className="shrink-0 rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1 text-[10px] font-bold text-gray-700">
          {isFetching ? 'Refreshing' : `${opportunities.length} matches`}
        </div>
      </div>

      {error ? (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-[8px] border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold text-red-700">
          <span>Live scan is reconnecting. Last available rows are shown below.</span>
          <button
            type="button"
            onClick={() => void refetch()}
            className="shrink-0 rounded-md border border-red-300 bg-white px-2 py-1 text-red-700"
          >
            Retry now
          </button>
        </div>
      ) : null}

      <div className="mt-3 w-full min-w-0 overflow-hidden rounded-[8px] border border-gray-200">
        <table className="w-full table-fixed border-collapse text-center text-[6px]">
          <thead className="bg-gray-100 text-[0.45rem] font-bold uppercase text-gray-800">
            <tr>
              {[
                'Rank', 'Instrument', 'Strike', 'Expiry', 'Type', 'GTP',
                'GTP Time', 'GTP Pass', 'LTP', 'Move', 'RSI', 'Premium High',
                'Premium Low', 'Support', 'Resistance', 'Filters Passed',
              ].map((label) => (
                <th key={label} className="px-px py-2 leading-tight">{label}</th>
              ))}
              {filterColumns.map((filter) => (
                <th key={filter.key} className="px-px py-2 leading-tight">{filter.label}</th>
              ))}
              <th className="px-px py-2 leading-tight">Score</th>
              <th className="px-px py-2 leading-tight">Signal Time</th>
              <th className="px-px py-2 leading-tight">Status</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td className="px-4 py-6 text-gray-700" colSpan={tableColumnCount}>
                  <span className="inline-flex items-center gap-2 font-semibold">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Loading market-wide S9 opportunities
                  </span>
                </td>
              </tr>
            ) : opportunities.length === 0 ? (
              <tr>
                <td className="px-4 py-6 font-semibold text-gray-700" colSpan={tableColumnCount}>
                  No S9 opportunities found yet.
                </td>
              </tr>
            ) : opportunities.map((item: any, index: number) => {
              const payload = item?.payload || {}
              const optionType = payload.selected_option_type || payload.final_option_type ||
                payload.option_type || payload.evaluated_option_type
              const strike = payload.selected_strike ?? payload.final_strike ??
                payload.strike ?? payload.evaluated_strike
              const passed = payload.passed_count ?? 0
              const total = payload.total_filters ?? 0
              const ltp = getSelectedStrikeLtp(payload, strike, optionType)
              const gtp = payload?.gtp ?? item?.gtp
              const gtpPassed = payload?.gtp_pass_count ?? payload?.gtp_passed_count ?? passed
              const gtpTotal = payload?.gtp_total_filters ?? total
              const expiry = payload?.strike_scan?.expiry || payload?.expiry
              const rsi = formatRsi(payload)
              const levels = premiumLevels(payload)
              const move = formatMove(ltp, gtp)
              const movePositive = move !== '-' && Number(ltp) - Number(gtp) >= 0

              return (
                <tr
                  key={`${item.id || index}-${item.symbol}-${strike}-${optionType}`}
                  className="border-t border-gray-200 text-gray-950"
                >
                  <td className="px-px py-2 font-bold">{index + 1}</td>
                  <td className="break-words px-px py-2 font-bold">
                    {payload.underlying_symbol || item.symbol || '-'}
                  </td>
                  <td className="px-px py-2 font-semibold">{formatStrike(strike, optionType)}</td>
                  <td className="px-px py-2 font-semibold">{formatExpiry(expiry)}</td>
                  <td className="px-px py-2 font-semibold">{optionTypeLabel(optionType)}</td>
                  <td className="px-px py-2 font-bold">{displayValue(gtp, 2)}</td>
                  <td className="px-px py-2 font-semibold">{formatTime(payload?.gtp_time)}</td>
                  <td className="px-px py-2 font-bold">{displayValue(gtpPassed)}/{displayValue(gtpTotal)}</td>
                  <td className="px-px py-2 font-bold">{displayValue(ltp, 2)}</td>
                  <td className={`px-px py-2 font-bold ${movePositive ? 'text-[#027A48]' : 'text-[#B42318]'}`}>
                    {move}
                  </td>
                  <td className="px-px py-2 font-bold">
                    <span>{rsi.value}</span>
                    {rsi.timeframe ? (
                      <span className="ml-1 rounded-full bg-gray-100 px-1 py-0.5 text-[0.5rem] font-bold text-gray-600">
                        {rsi.timeframe}
                      </span>
                    ) : null}
                  </td>
                  <td className="px-px py-2 font-semibold">{displayValue(levels.high, 2)}</td>
                  <td className="px-px py-2 font-semibold">{displayValue(levels.low, 2)}</td>
                  <td className="px-px py-2 font-bold text-[#027A48]">{displayValue(levels.support, 2)}</td>
                  <td className="px-px py-2 font-bold text-[#B42318]">{displayValue(levels.resistance, 2)}</td>
                  <td className="px-px py-2 font-bold">{displayValue(passed)}/{displayValue(total)}</td>
                  {filterColumns.map((filter) => (
                    <td key={filter.key} className="px-px py-2">
                      <FilterBadge status={filterStatus(payload, filter.key)} />
                    </td>
                  ))}
                  <td className="px-px py-2 font-semibold">{formatScore(payload, item)}</td>
                  <td className="px-px py-2 font-semibold">{formatTime(payload.signal_time)}</td>
                  <td className="px-px py-2">
                    <TradeStatus value={payload.trade_status || 'UNKNOWN'} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </article>
  )
}

export default S9Screen
