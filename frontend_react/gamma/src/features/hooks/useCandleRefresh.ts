import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query"


export const useCandleRefresh = () => {
  const queryClient = useQueryClient();

  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout>

    const scheduleNextRefresh = () => {
      const now = new Date();

      const minutes = now.getMinutes();
      const seconds = now.getSeconds();

      // Next 5m candle boundary
      const next5Min = Math.ceil(minutes / 5) * 5;

      const next = new Date(now);

      next.setMinutes(next5Min);
      next.setSeconds(2); // wait 2 sec after candle close
      next.setMilliseconds(0);

      // handle hour rollover
      if (next <= now) {
        next.setMinutes(next.getMinutes() + 5);
      }

      const delay = next.getTime() - now.getTime();

      timeout = setTimeout(async () => {
        console.log("Refreshing after candle close...");

        await queryClient.invalidateQueries();

        scheduleNextRefresh();
      }, delay);
    };

    scheduleNextRefresh();

    return () => clearTimeout(timeout);
  }, [queryClient]);
};