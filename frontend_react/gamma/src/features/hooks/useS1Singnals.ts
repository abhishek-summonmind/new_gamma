import { useQuery } from "@tanstack/react-query";
import { getS1Signals } from "../../services/s1SignalsApi";
export const useS1Signals = () => {
  return useQuery({
    queryKey: ["s1Signals"],
    queryFn: getS1Signals,
  });
};