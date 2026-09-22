import { api } from "./client";

export const getHealth = async () => {
    const res = await api.get("/health")
    // console.log("res.data: ",res.data)
    return res.data;
}