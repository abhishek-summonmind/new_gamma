interface TimeframeSignal {
  timeframe: string;
  bullish: number;
  bearish: number;
  neutral: number;
}

interface SignalResult {
  totals: {
    bullish: number;
    bearish: number;
  };

  timeframeSignals: TimeframeSignal[];
}

const indexSymbols = new Set(["NIFTY 50", "NIFTY50", "BANK NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"])
const isIndexSymbol = (value: unknown) => {
  const normalized = String(value || "").trim().toUpperCase().replace(/\s+/g, " ")
  return indexSymbols.has(normalized) || indexSymbols.has(normalized.replace(/\s+/g, ""))
}

export const calculateSignals = (items: any[]): SignalResult => {
  const timeframeMap: Record<string, TimeframeSignal> = {
    "5m": {
      timeframe: "5m",
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },

    "10m": {
      timeframe: "10m",
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },

    "15m": {
      timeframe: "15m",
      bullish: 0,
      bearish: 0,
      neutral: 0,
    },
  };

  let totalBullish = 0;
  let totalBearish = 0;

  const filteredItems = items.filter((item) => isIndexSymbol(item?.symbol))

  filteredItems.forEach((item) => {
    const signal = item?.signal;

    // strong_buy → bearish in 10m + 15m
    if (signal === "strong_buy") {
        timeframeMap["10m"].bullish += 1;
        timeframeMap["15m"].bullish += 1;

        totalBullish += 2;
    }
    
    // strong_sell → bullish in 10m + 15m
    if (signal === "strong_sell") {
      timeframeMap["10m"].bearish += 1;
      timeframeMap["15m"].bearish += 1;

      totalBearish += 2;
    }

    // buy → bearish in 5m
    if (signal === "buy") {
        timeframeMap["5m"].bullish += 1;
        
        totalBullish += 1;
    }
    
    // sell → bullish in 5m
    if (signal === "sell") {
      timeframeMap["5m"].bearish += 1;

      totalBearish += 1;
    }
  });

  return {
    totals: {
      bullish: totalBullish,
      bearish: totalBearish,
    },

    timeframeSignals: Object.values(timeframeMap),
  };
};
