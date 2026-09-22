import { useQuery } from "@tanstack/react-query";
import { getS1 } from "../../services/S1Api";

export const useS1 = () => {
  return useQuery({
    queryKey: ["s1"],
    queryFn: getS1,
  });
};