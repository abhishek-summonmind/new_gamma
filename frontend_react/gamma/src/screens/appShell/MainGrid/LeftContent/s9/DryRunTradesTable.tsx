import { useState } from 'react'
import { Download, Loader2, ReceiptText } from 'lucide-react'

import { useDryRunTrades } from '../../../../../features/hooks/useDryRunTrades'
import type { DryRunTrade } from '../../../../../services/dryRunTradesApi'

const columns = [
  'Instrument',
  'Strike',
  'Type',
  'Entry Time',
  'Entry Price',
  'Entry Score',
  'Entry Pass',
  'SL',
  'Status',
  'Exit Time',
  'Exit Price',
]

const displayValue = (value: string | number | null, digits?: number) => {
  if (value === null || value === '') return '-'
  if (typeof value === 'number' && Number.isFinite(value)) {
    return digits === undefined ? value.toLocaleString('en-IN') : value.toFixed(digits)
  }
  return String(value)
}

const formatTime = (value: string | null) => {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  return parsed.toLocaleTimeString('en-IN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

const marketDate = (value: Date) => {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

const tradeDate = (value: string | null) => {
  if (!value) return ''
  // Backend timestamps are market-time ISO strings, so preserve their date part.
  const datePart = value.match(/^\d{4}-\d{2}-\d{2}/)?.[0]
  return datePart ?? ''
}

const csvValue = (value: string | number | null) => {
  const text = value === null ? '' : String(value)
  return `"${text.replace(/"/g, '""')}"`
}

const StatusBadge = ({ status }: { status: string }) => {
  const normalized = String(status || 'UNKNOWN').toUpperCase()
  const colors = normalized === 'OPEN'
    ? 'border-[#ABEFC6] bg-[#ECFDF3] text-[#027A48]'
    : normalized === 'CLOSED'
      ? 'border-gray-300 bg-gray-100 text-gray-700'
      : 'border-[#FEDF89] bg-[#FFFAEB] text-[#B54708]'
  return (
    <span className={`inline-flex rounded-full border px-2 py-1 text-[10px] font-bold ${colors}`}>
      {normalized}
    </span>
  )
}

const TradeRow = ({ trade }: { trade: DryRunTrade }) => (
  <tr className="border-t border-gray-200 text-gray-950">
    <td className="px-2 py-2.5 font-bold">{displayValue(trade.instrument)}</td>
    <td className="px-2 py-2.5 font-semibold">{displayValue(trade.strike)}</td>
    <td className="px-2 py-2.5 font-semibold">{displayValue(trade.type)}</td>
    <td className="px-2 py-2.5 font-semibold">{formatTime(trade.entry_time)}</td>
    <td className="px-2 py-2.5 font-semibold">{displayValue(trade.entry_price, 2)}</td>
    <td className="px-2 py-2.5 font-bold text-[#175CD3]">{displayValue(trade.entry_score, 2)}</td>
    <td className="px-2 py-2.5 font-bold">{displayValue(trade.entry_pass)}</td>
    <td className="px-2 py-2.5 font-semibold text-[#B42318]">{displayValue(trade.sl, 2)}</td>
    <td className="px-2 py-2.5"><StatusBadge status={trade.status} /></td>
    <td className="px-2 py-2.5 font-semibold">{formatTime(trade.exit_time)}</td>
    <td className="px-2 py-2.5 font-semibold">{displayValue(trade.exit_price, 2)}</td>
  </tr>
)

export default function DryRunTradesTable() {
  const { data, error, isLoading, isFetching, refetch } = useDryRunTrades()
  const [selectedDate, setSelectedDate] = useState(() => marketDate(new Date()))
  const trades = Array.isArray(data?.items)
    ? data.items.filter((trade) => {
      const status = String(trade.status).toUpperCase()
      return tradeDate(trade.entry_time) === selectedDate && (status === 'OPEN' || status === 'CLOSED')
    })
    : []

  const downloadTrades = () => {
    const rows = trades.map((trade) => [
      trade.instrument,
      trade.strike,
      trade.type,
      formatTime(trade.entry_time),
      trade.entry_price,
      trade.entry_score,
      trade.entry_pass,
      trade.sl,
      String(trade.status).toUpperCase(),
      formatTime(trade.exit_time),
      trade.exit_price,
    ])
    const csv = [columns, ...rows]
      .map((row) => row.map((value) => csvValue(value)).join(','))
      .join('\r\n')
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `dry-run-trades-${selectedDate}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <section className="mt-6 border-t border-gray-200 pt-5" aria-labelledby="dry-run-trades-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ReceiptText className="h-5 w-5 shrink-0 text-[#175CD3]" />
            <h2 id="dry-run-trades-heading" className="text-lg font-bold text-gray-950">Dry Run Trades</h2>
          </div>
          <p className="mt-1 text-xs font-medium text-gray-600">Open and closed simulated trades for the selected date. No live orders are placed.</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <label className="flex items-center gap-2 text-[10px] font-bold uppercase text-gray-700">
            Trade date
            <input
              type="date"
              value={selectedDate}
              max={marketDate(new Date())}
              onChange={(event) => setSelectedDate(event.target.value)}
              className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs font-semibold normal-case text-gray-900"
              aria-label="Filter dry run trades by date"
            />
          </label>
          <div className="rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1 text-[10px] font-bold text-gray-700">
            {isFetching ? 'Refreshing' : `${trades.length} trades`}
          </div>
          <button
            type="button"
            onClick={downloadTrades}
            disabled={trades.length === 0}
            className="inline-flex items-center gap-1.5 rounded-md border border-[#175CD3] bg-[#175CD3] px-2.5 py-1.5 text-xs font-bold text-white disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-300"
          >
            <Download className="h-3.5 w-3.5" />
            Download CSV
          </button>
        </div>
      </div>

      {error ? (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-[8px] border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold text-red-700">
          <span>Dry run trades could not be refreshed.</span>
          <button type="button" onClick={() => void refetch()} className="shrink-0 rounded-md border border-red-300 bg-white px-2 py-1 text-red-700">
            Retry now
          </button>
        </div>
      ) : null}

      <div className="mt-3 w-full min-w-0 overflow-x-auto rounded-[8px] border border-gray-200">
        <table className="w-full min-w-[900px] border-collapse text-center text-[11px]">
          <thead className="bg-gray-100 text-[9px] font-bold uppercase text-gray-800">
            <tr>
              {columns.map((column) => <th key={column} className="whitespace-nowrap px-2 py-2.5">{column}</th>)}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td className="px-4 py-6 text-gray-700" colSpan={columns.length}>
                  <span className="inline-flex items-center gap-2 font-semibold">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Loading dry run trades
                  </span>
                </td>
              </tr>
            ) : trades.length === 0 ? (
              <tr>
                <td className="px-4 py-6 font-semibold text-gray-600" colSpan={columns.length}>No dry run trades for the selected date.</td>
              </tr>
            ) : trades.map((trade) => <TradeRow key={trade.id} trade={trade} />)}
          </tbody>
        </table>
      </div>
    </section>
  )
}
