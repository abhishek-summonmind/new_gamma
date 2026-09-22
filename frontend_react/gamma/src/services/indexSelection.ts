export const SUPPORTED_INDEX_SYMBOLS = ['NIFTY 50', 'BANK NIFTY', 'SENSEX', 'FINNIFTY', 'DIXON', 'HDFCAMC', 'SRF', 'HAL', 'APOLLOHOSP', 'SUNPHARMA', 'ICICIBANK', 'LT', 'POLYCAB', 'SOLARINDS', 'TITAN'] as const
export const SUPPORTED_MARKET_SYMBOLS = SUPPORTED_INDEX_SYMBOLS

export type IndexSymbol = typeof SUPPORTED_INDEX_SYMBOLS[number]

const STORAGE_KEY = 'gamma:selected-index'

export const normalizeIndexSymbol = (value?: string | null): IndexSymbol => {
  const normalized = String(value || '').trim().toUpperCase().replace(/[\s_-]+/g, '')
  if (normalized === 'SENSEX' || normalized === 'BSESENSEX') return 'SENSEX'
  if (normalized === 'BANKNIFTY') return 'BANK NIFTY'
  if (normalized === 'FINNIFTY' || normalized === 'FINNIFTY50') return 'FINNIFTY'
  const aliases: Record<string, IndexSymbol> = { HDFCAMC: 'HDFCAMC', HINDUSTANAERONAUTICS: 'HAL', APOLLOHOSPITALS: 'APOLLOHOSP', SUNPHARMA: 'SUNPHARMA', ICICI: 'ICICIBANK', ICICIBANK: 'ICICIBANK', LARSENANDTOUBRO: 'LT', 'L&T': 'LT', POLYCABINDIA: 'POLYCAB', SOLARINDUSTRIES: 'SOLARINDS', SOLARINDS: 'SOLARINDS', TITANCOMPANY: 'TITAN' }
  if (aliases[normalized] || (SUPPORTED_MARKET_SYMBOLS as readonly string[]).includes(normalized)) return (aliases[normalized] || normalized) as IndexSymbol
  return 'NIFTY 50'
}

export const normalizeMarketSymbol = normalizeIndexSymbol

export const isSupportedIndexSymbol = (value?: string | null) => {
  const raw = String(value || '').trim()
  if (!raw) return false
  const normalized = raw.toUpperCase().replace(/[\s_-]+/g, '')
  return normalized === 'NIFTY50' || normalized === 'NIFTY' || normalized === 'SENSEX' || normalized === 'BSESENSEX' || normalized === 'BANKNIFTY' || normalized === 'FINNIFTY' || (SUPPORTED_MARKET_SYMBOLS as readonly string[]).some((item) => item.replace(/\s/g, '') === normalized)
}
export const isSupportedMarketSymbol = isSupportedIndexSymbol

export const getSelectedIndexSymbol = (): IndexSymbol => {
  if (typeof window === 'undefined') return 'NIFTY 50'
  return normalizeIndexSymbol(window.localStorage.getItem(STORAGE_KEY))
}

export const setSelectedIndexSymbol = (symbol: string): IndexSymbol => {
  const selected = normalizeIndexSymbol(symbol)
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(STORAGE_KEY, selected)
    window.dispatchEvent(new CustomEvent('gamma:index-symbol-change', { detail: selected }))
  }
  return selected
}

export const getSelectedMarketSymbol = getSelectedIndexSymbol
export const setSelectedMarketSymbol = setSelectedIndexSymbol
