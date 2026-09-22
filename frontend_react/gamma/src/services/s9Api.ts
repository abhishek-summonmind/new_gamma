import { api } from "./client";

const s9AdminHeaders = () => {
  const key = import.meta.env.VITE_REPORTS_API_KEY;
  return key ? { "X-API-Key": key } : undefined;
};

export const getS9 = async ({ signal }: { signal?: AbortSignal } = {}) => {
  const res = await api.get("/screener/s9", {
    params: { limit: 6, ltp_refresh: true },
    signal,
    timeout: 12000,
  });
  return res.data;
};

export const getS9Override = async () => {
  const res = await api.get("/screener/s9/override");
  return res.data;
};

export const setS9Override = async (manual_direction: "bullish" | "bearish") => {
  const res = await api.put("/screener/s9/override", {
    manual_direction,
    updated_by: "dashboard",
  }, {
    headers: s9AdminHeaders(),
  });
  return res.data;
};

export const resetS9Override = async () => {
  const res = await api.delete("/screener/s9/override", {
    params: { updated_by: "dashboard" },
    headers: s9AdminHeaders(),
  });
  return res.data;
};
