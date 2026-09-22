import { create } from "zustand";

interface MarketState {
  nifty: number;
  marketBias: string;

  setNifty: (value: number) => void;
  setMarketBias: (value: string) => void;
}

export const useMarketStore = create<MarketState>((set) => ({
  nifty: 22735.45,
  marketBias: "BULLISH",

  setNifty: (value) => set({ nifty: value }),

  setMarketBias: (value) =>
    set({
      marketBias: value,
    }),
}));