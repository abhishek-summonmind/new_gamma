interface TimeframeSignal {
  timeframe: string

  bullish: number
  bearish: number
  neutral: number
}

interface SignalResult {

  totals: {
    bullish: number
    bearish: number
    neutral: number
  }

  timeframeSignals: TimeframeSignal[]
}

export const calculateSignals = (
  items: any[],
): SignalResult => {

  const timeframeMap:
    Record<string, TimeframeSignal> = {

    '5m': {
      timeframe: '5m',
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },

    '10m': {
      timeframe: '10m',
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },

    '15m': {
      timeframe: '15m',
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },

  }

  let totalBullish = 0
  let totalBearish = 0
  let totalNeutral = 0

  items.forEach((item) => {

    const signal = item?.signal

    // BUY
    if (signal === 'buy') {

      timeframeMap['5m']
        .bullish += 1

      totalBullish += 1

    }

    // SELL
    else if (signal === 'sell') {

      timeframeMap['5m']
        .bearish += 1

      totalBearish += 1

    }

    // STRONG BUY
    else if (
      signal === 'strong_buy'
    ) {

      timeframeMap['10m']
        .bullish += 1

      timeframeMap['15m']
        .bullish += 1

      totalBullish += 2

    }

    // STRONG SELL
    else if (
      signal === 'strong_sell'
    ) {

      timeframeMap['10m']
        .bearish += 1

      timeframeMap['15m']
        .bearish += 1

      totalBearish += 2

    }

    // NEUTRAL
    else {

      timeframeMap['5m']
        .neutral += 1

      timeframeMap['10m']
        .neutral += 1

      timeframeMap['15m']
        .neutral += 1

      totalNeutral += 3

    }

  })

  return {

    totals: {

      bullish: totalBullish,

      bearish: totalBearish,

      neutral: totalNeutral,

    },

    timeframeSignals:
      Object.values(timeframeMap),

  }

}