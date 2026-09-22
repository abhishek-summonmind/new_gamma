import { useMutation } from "@tanstack/react-query";
import { refresh } from "../../services/refreshApi";

export const useRefresh = () => {

  return useMutation({
    mutationFn: refresh,
  })

}