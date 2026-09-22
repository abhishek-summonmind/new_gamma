import { useMemo, useState } from 'react'
import { Download } from 'lucide-react'
import { useAlert } from '../../../../../features/hooks/useAlert'
import { downloadReport } from '../../../../../services/documentApi'

type AlertSource = {
  screener: string
  signal: string
  confidence: number
  reason: string
}

type AlertItem = {
  id: number
  symbol: string
  company_name: string
  alert_type: string
  action: 'buy' | 'sell' | 'neutral'
  confidence: number
  message: string

  payload: {
    source_screener: string
    signal: string

    buy_score: number
    sell_score: number
    trap_score: number

    sources: AlertSource[]
  }

  is_active: boolean
  created_at: string
}

function AlertsScreen() {
  const {data,isLoading} = useAlert();
  const [isDownloading,setisDownloading] = useState(false)
  console.log("alerts is alerts screen: ",data);
  // console.log(reportData)



 const sortedAlerts = useMemo<
  AlertItem[]
>(() => {

  if (!data?.items) {
    return []
  }

  // REMOVE INVALID / INACTIVE
  const filteredAlerts =
    data.items.filter(
      (item: AlertItem) => {

        return (
          item.is_active &&
          item.symbol &&
          item.payload
        )

      },
    )

  // SORT BY CONFIDENCE
  return filteredAlerts.sort(
    (a: AlertItem, b: AlertItem) =>

      b.confidence -
      a.confidence,
  )

}, [data])

  const handleDownloadCSV =
  async () => {

    try {
      setisDownloading(true)
      const blob =
        await downloadReport()

      const url =
        window.URL.createObjectURL(
          blob,
        )

      const link =
        document.createElement('a')

      link.href = url

      link.setAttribute(
        'download',
        'alerts_report.csv',
      )

      document.body.appendChild(
        link,
      )

      link.click()

      link.remove()

      window.URL.revokeObjectURL(
        url,
      )

      setisDownloading(false)

    } catch (error) {
      setisDownloading(false)
      console.error(
        'CSV download failed:',
        error,
      )

    }

}

  const getActionStyle = (
    action: string,
  ) => {

    if (action === 'buy') {

      return `
        bg-emerald-500
        text-white
      `
    }

    if (action === 'sell') {

      return `
        bg-[#ff5b45]
        text-white
      `
    }

    return `
      bg-yellow-500
      text-black
    `
  }

  return (
    <div
      className="
        min-h-screen
        bg-[#050f1d]
        px-2
        py-1
      "
    >

      {/* HEADER */}
      <div
        className="
          flex
          flex-col
          gap-2

          md:flex-row
          md:items-center
          md:justify-between
        "
      >

        <div>

          <h1
            className="
              text-xl
              font-bold
              tracking-[0.02em]
              text-white
            "
          >
            Live Alerts
          </h1>

          <p
            className="
              mt-1
              text-sm
              text-white/50
            "
          >
            Real-time screener alerts
            and notifications
          </p>

        </div>

        {/* DOWNLOAD BUTTON */}
        <button
          onClick={handleDownloadCSV}
          className="
            inline-flex
            items-center
            gap-2
            rounded-xl
            border
            border-cyan-500/20
            bg-cyan-500/10
            px-4
            py-2.5
            text-sm
            font-semibold
            text-cyan-400
            transition-all
            duration-200

            hover:border-cyan-400/40
            hover:bg-cyan-500/15
          "
        >

          <Download
            size={16}
          />

          {isDownloading ? 'Downloading ...' : 'Download Alerts CSV'}

        </button>

      </div>

      {/* ALERTS */}
      <div
        className="
          mt-2
          grid
          grid-cols-1
          md:grid-cols-2
          gap-4
        "
      >

        {sortedAlerts.map((alert) => {

          return (

            <div
              key={alert.id}
              className="
                relative
                overflow-hidden
                rounded-[14px]
                border
                border-[#17314d]
                bg-[linear-gradient(180deg,#08192c_0%,#071422_100%)]
                px-4
                py-2
                shadow-[0_12px_40px_rgba(0,0,0,0.30)]
              "
            >

              {/* TOP GLOW */}
              <div
                className="
                  absolute
                  inset-x-0
                  top-0
                  h-px
                  bg-cyan-400/25
                "
              />

              {/* ACTION BADGE */}
              <div
                className={`
                  absolute
                  right-4
                  top-3

                  rounded-full
                  px-3
                  py-1

                  text-[11px]
                  font-bold
                  uppercase
                  tracking-[0.08em]

                  ${getActionStyle(
                    alert.action,
                  )}
                `}
              >
                {alert.action}
              </div>

              {/* MAIN GRID */}
              <div
                className="
                  grid
                  gap-6
                  md:grid-cols-[1fr_320px]
                "
              >

                {/* LEFT */}
                <div>

                  {/* SYMBOL */}
                  <h2
                    className="
                      text-md
                      font-bold
                      uppercase
                      tracking-[0.03em]
                      text-white
                    "
                  >
                    {alert.symbol}
                  </h2>

                  {/* MESSAGE */}
                  <p
                    className="
                      text-xs
                      font-medium
                      text-white/80
                    "
                  >
                    {alert.message}
                  </p>

                  {/* DETAILS */}
                  <div
                    className="
                      mt-2
                      space-y-2
                    "
                  >

                    <div
                      className="
                        flex
                        items-center
                        gap-2
                        text-xs
                      "
                    >

                      <span
                        className="
                          text-white/55
                        "
                      >
                        Confidence:
                      </span>

                      <span
                        className="
                          font-semibold
                          text-cyan-300
                        "
                      >
                        {alert.confidence.toFixed(
                          4,
                        )}
                      </span>

                    </div>

                    <div
                      className="
                        flex
                        items-center
                        gap-2
                        text-xs
                      "
                    >

                      <span
                        className="
                          text-white/55
                        "
                      >
                        Buy Score:
                      </span>

                      <span
                        className="
                          font-semibold
                          text-emerald-400
                        "
                      >
                        {alert.payload.buy_score.toFixed(
                          4,
                        )}
                      </span>

                    </div>

                    <div
                      className="
                        flex
                        items-center
                        gap-2
                        text-xs
                      "
                    >

                      <span
                        className="
                          text-white/55
                        "
                      >
                        Trap Score:
                      </span>

                      <span
                        className="
                          font-semibold
                          text-yellow-300
                        "
                      >
                        {alert.payload.trap_score.toFixed(
                          4,
                        )}
                      </span>

                    </div>

                    <div
                      className="
                        flex
                        items-center
                        gap-2
                        text-xs
                      "
                    >

                      <span
                        className="
                          text-white/55
                        "
                      >
                        Created at:
                      </span>

                      <span
                        className="
                          font-semibold
                          text-white
                        "
                      >
                        {
  new Date(
    `${alert.created_at}Z`,
  ).toLocaleTimeString(
    'en-IN',
    {
      timeZone: 'Asia/Kolkata',

      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',

      hour12: false,
    },
  )
}
                      </span>

                    </div>

                    

                  </div>

                </div>

                {/* RIGHT */}
                <div
                  className="
                    flex
                    flex-col
                    justify-center
                    gap-3
                  "
                >

                  <div
                    className="
                      rounded-xl
                      border
                      border-[#17314d]
                      bg-[#08182a]
                      px-4
                      py-2
                    "
                  >

                    <div
                      className="
                        text-xs
                        text-white/50
                      "
                    >
                      Screeners
                    </div>

                    <div
                      className="
                        mt-1
                        text-sm
                        font-semibold
                        text-white
                      "
                    >
                      {
                        alert.payload
                          .source_screener
                      }
                    </div>

                  </div>

                  <div
                    className="
                      rounded-xl
                      border
                      border-[#17314d]
                      bg-[#08182a]
                      px-4
                      py-2
                    "
                  >

                    <div
                      className="
                        text-xs
                        text-white/50
                      "
                    >
                      Sell Score
                    </div>

                    <div
                      className="
                        text-sm
                        font-semibold
                        text-[#ff5b45]
                      "
                    >
                      {alert.payload.sell_score.toFixed(
                        4,
                      )}
                    </div>

                  </div>

                  

                </div>

              </div>

            </div>

          )

        })}

      </div>

    </div>
  )
}

export default AlertsScreen
