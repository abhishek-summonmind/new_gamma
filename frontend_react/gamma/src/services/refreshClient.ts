import axios from "axios";

export const refreshClientApi = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,
  timeout: 300000,
})