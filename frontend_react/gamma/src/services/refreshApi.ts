import { refreshClientApi } from "./refreshClient";
import { getSelectedIndexSymbol } from "./indexSelection";
import { useConfigStore } from "../stores/configStore";

export const refresh = async (symbol?: string) => {
    const res = await refreshClientApi.post("/refresh", undefined, {
        params: { force: true, symbol: symbol || getSelectedIndexSymbol() },
    })
    const resolvedExpiry = res.data?.resolved_expiry;
    if (typeof resolvedExpiry === "string" && resolvedExpiry) {
        useConfigStore.getState().setOptionExpiry(resolvedExpiry);
    }
    // console.log("res.data: ",res.data)
    return res.data;
}
