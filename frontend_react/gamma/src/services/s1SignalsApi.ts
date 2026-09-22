import { api } from "./client";

export const getS1Signals = async () => {
    const res = await api.get("/s1-signals?limit=50")
    // console.log("res.data: ",res.data)
    return res.data;
}