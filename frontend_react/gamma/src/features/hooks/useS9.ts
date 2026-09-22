import { useQuery } from "@tanstack/react-query";
import { getS9 } from "../../services/s9Api";

const placeholderItem = (symbol: "NIFTY 50" | "SENSEX") => ({
  symbol,
  signal: "neutral",
  confidence: 0,
  reason: "DATA_PENDING",
  payload: {
    underlying_symbol: symbol,
    passed_count: 0,
    total_filters: 0,
    score: 0,
    data_pending: true,
  },
});

const initialS9Data = {
  screener: "S9",
  scope: "market_universe",
  refreshing: true,
  count: 2,
  items: [placeholderItem("NIFTY 50"), placeholderItem("SENSEX")],
};

const LTP_REFRESH_INTERVAL_MS = 10_000;

export const useS9 = () => {
  return useQuery({
    queryKey: ["s9"],
    queryFn: getS9,
    refetchInterval: LTP_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    staleTime: 0,
    placeholderData: initialS9Data,
    retry: 2,
  });
};
