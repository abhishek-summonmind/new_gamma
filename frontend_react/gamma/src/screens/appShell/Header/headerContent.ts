export type HeaderActionKind = 'alerts' | 'settings' | 'fullscreen' | 'logout'

export type HeaderStatusTone = 'green' | 'blue'

export const brandSectionData = {
  name: 'GAMMA',
  version: 'v3.0',
  subtitle: 'Real-Time Intraday Dashboard',
  connectionLabel: 'Strategy desk connected',
}

export const marketHeroData = {
  instrument: 'NIFTY 50',
  marketLabel: 'Index options pulse',
  price: '22,735.45',
  change: '+178.35',
  changePercent: '+0.79%',
}

export const marketDetailStats = [
  { label: 'Open', value: '22,589.35' },
  { label: 'High', value: '22,812.40' },
  { label: 'Low', value: '22,517.80' },
  { label: 'Prev Close', value: '22,557.10' },
]

export const timeWidgetData = {
  label: 'Time (IST)',
  time: '11:24:36',
  dateLabel: 'Wed, 13 May 2026',
}

export const statusWidgetData = {
    label: 'Market Status',
    value: 'LIVE',
    detail: 'Open 09:15 - 15:30',
    tone: 'green',
  }

export const CandleData = {
    label: 'Next Candle',
    value: '5m',
    detail: '00:00:24',
    tone: 'blue',
  }


export const headerActions: Array<{
  kind: HeaderActionKind
  label: string
  badge?: string
  emphasis?: 'default' | 'danger'
}> = [
  { kind: 'alerts', label: 'Alerts', badge: '12' },
  { kind: 'settings', label: 'Settings' },
  { kind: 'fullscreen', label: 'Fullscreen' },
  { kind: 'logout', label: 'Logout', emphasis: 'danger' },
]
