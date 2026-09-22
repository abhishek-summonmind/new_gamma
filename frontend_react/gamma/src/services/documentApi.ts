import { api } from "./client"

export const downloadReport =
  async () => {

    const res = await api.get(
      "/reports/alerts.csv?limit=500",
      {

        responseType: 'blob',

        timeout: 0,

      },
    )

    return res.data

}