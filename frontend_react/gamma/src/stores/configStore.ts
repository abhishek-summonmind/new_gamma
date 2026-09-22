import { create } from "zustand";

interface GlobalConfigState {
  optionExpiry: string;
  setOptionExpiry: (expiry: string) => void;
}

export const useConfigStore = create<GlobalConfigState>((set) => ({
  optionExpiry: "",
  setOptionExpiry: (expiry) =>
    set({
      optionExpiry: expiry,
    }),
}));