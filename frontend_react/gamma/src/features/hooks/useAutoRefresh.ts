import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

export const useAutoRefresh = () => {
  const queryClient = useQueryClient();

  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout>;
    let stopped = false;

    const scheduleRefresh = () => {
      timeout = setTimeout(() => {
        // The backend scheduler owns the expensive market scan. The browser
        // only polls active cache-backed queries. Posting /refresh every ten
        // seconds caused overlapping full scans and a permanently busy UI.
        void queryClient.invalidateQueries({
          refetchType: "active",
          predicate: (query) => query.queryKey[0] !== "s9",
        });
        if (!stopped) scheduleRefresh();
      }, 10000);
    };

    scheduleRefresh();

    return () => {
      stopped = true;
      clearTimeout(timeout);
    };
  }, [queryClient]);
};
