import { useQuery } from "@tanstack/react-query";
import { alert } from "../../services/alertApi";

export const useAlert = () => {
  return useQuery({
    queryKey: ["alert"],
    queryFn: alert,
  });
};