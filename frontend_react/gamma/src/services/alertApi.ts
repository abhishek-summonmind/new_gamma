import { api } from "./client";

export const alert = async () => {
    const res = await api.get("/alerts?limit=100")
    // console.log("res.data: ",res.data)
    return res.data;
}