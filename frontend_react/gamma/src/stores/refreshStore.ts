import { create } from "zustand"

interface RefreshStore {

  isRefreshing: boolean

  setIsRefreshing: (
    value: boolean
  ) => void

}

export const useRefreshStore =
  create<RefreshStore>((set) => ({

    isRefreshing: false,

    setIsRefreshing: (value) =>
      set({
        isRefreshing: value,
      }),

  }))