import { api } from "./client";
import { getSelectedMarketSymbol, isSupportedMarketSymbol, normalizeMarketSymbol } from "./indexSelection";

type S1Item = {
    symbol?: string | null
}

export const getS1 = async () => {
    const selected = getSelectedMarketSymbol()
    const res = await api.get("/screener/s1", { params: { limit: 50, symbol: selected } })
    const data = res.data
    if (data && Array.isArray(data.items)) {
        data.items = data.items.filter((item: S1Item) => isSupportedMarketSymbol(item?.symbol) && normalizeMarketSymbol(item?.symbol) === selected)
        data.count = data.items.length
    }
    return data;
}
