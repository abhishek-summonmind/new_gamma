import { create } from "zustand";

export type MinuteType = "5m" | "10m" | "15m";

interface MinuteState {
  minute: string;

  setMinute: (value: string) => void;
}

export const useMinuteStore = create<MinuteState>((set) => ({
  minute: "5m",

  setMinute: (value) =>
    set({
      minute: value,
    }),
}));