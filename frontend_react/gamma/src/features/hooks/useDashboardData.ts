import { useQueries } from "@tanstack/react-query";
import { getS1 } from "../../services/S1Api";
import { getS9 } from "../../services/s9Api";
import { alert } from "../../services/alertApi";

export const useDashboardData = () => {
  return useQueries({
    queries: [
      {
        queryKey: ["s1"],
        queryFn: getS1,
      },
      {
        queryKey: ["s9"],
        queryFn: getS9,
      },
      {
        queryKey: ["alert"],
        queryFn: alert,
      },
    ],
  });
};