import { useQuery } from '@tanstack/react-query'

import { getDryRunTrades } from '../../services/dryRunTradesApi'

const TRADE_REFRESH_INTERVAL_MS = 10_000

export const useDryRunTrades = () => useQuery({
  queryKey: ['dry-run-trades'],
  queryFn: getDryRunTrades,
  refetchInterval: TRADE_REFRESH_INTERVAL_MS,
  refetchIntervalInBackground: true,
  staleTime: 0,
  retry: 2,
})
