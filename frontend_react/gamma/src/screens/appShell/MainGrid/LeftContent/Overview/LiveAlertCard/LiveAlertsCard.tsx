import { useMemo } from "react"
import { useAlert } from "../../../../../../features/hooks/useAlert"

type DynamicAlert = {

  id: number

  time: string

  tag: string

  stock: string

  timeframe: string

  status: string

  description: string

  color: string

}

function LiveAlertsCard() {
  const { data , isLoading } = useAlert();
  console.log("alerts: ", data)

 const dynamicAlerts =
  useMemo<DynamicAlert[]>(() => {

  if (!data?.items) {
    return []
  }

  return data.items

    // ACTIVE ONLY
    .filter((item: any) => {

      return (
        item.is_active
        &&
        item.symbol
      )

    })

    // SORT LATEST FIRST
    .sort(
      (a: any, b: any) =>

        new Date(
          b.created_at,
        ).getTime()

        -

        new Date(
          a.created_at,
        ).getTime(),
    )

    // MAP UI DATA
    .map((item: any) => {

      const action =
        item?.action || ''

      const signal =
        item?.payload
          ?.signal || ''

      const source =
        item?.payload
          ?.source_screener || ''

      // COLOR
      let color = 'white'

      if (
        action.includes('buy')
      ) {

        color = 'green'

      }

      else if (
        action.includes('sell')
      ) {

        color = 'red'

      }

      else if (
        action.includes('watch')
      ) {

        color = 'yellow'

      }

      // STATUS
      let status = 'NEUTRAL'

      if (
        signal.includes(
          'strong_buy',
        )
      ) {

        status = 'STRONG BUY'

      }

      else if (
        signal.includes(
          'strong_sell',
        )
      ) {

        status = 'STRONG SELL'

      }

      else if (
        signal.includes('buy')
      ) {

        status = 'BULLISH'

      }

      else if (
        signal.includes('sell')
      ) {

        status = 'BEARISH'

      }

      else if (
        signal.includes(
          'watch',
        )
      ) {

        status = 'WATCH'

      }

      return {

        id: item.id,

        time:
  new Date(
    `${item.created_at}Z`,
  ).toLocaleTimeString(
    'en-IN',
    {
      timeZone: 'Asia/Kolkata',

      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',

      hour12: false,
    },
  ),

        tag: source,

        stock:
          item.symbol,

        timeframe:
          signal.includes(
            '5m',
          )
            ? '5m'
            : signal.includes(
                '10m',
              )
            ? '10m'
            : signal.includes(
                '15m',
              )
            ? '15m'
            : '',

        status,

        description:
          item.message,

        color,
      }

    })

}, [data])

  return (
    <article
      className="
        relative
        h-full
        overflow-hidden
        rounded-[12px]
        border
        border-[#17314d]
        bg-[linear-gradient(180deg,#081a2d_0%,#071524_100%)]
        shadow-[0_18px_50px_rgba(0,0,0,0.35)]
      "
    >
      {/* GRID TEXTURE */}
      <div
        className="
          pointer-events-none
          absolute
          inset-0
          opacity-[0.03]
          [background-image:linear-gradient(rgba(255,255,255,0.08)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.08)_1px,transparent_1px)]
          [background-size:26px_26px]
        "
      />

      {/* TOP GLOW */}
      {/* <div className="absolute inset-x-0 top-0 h-px bg-cyan-400/30" /> */}

      <div className="relative z-10 h-full p-3">

        {/* HEADER */}
        <div className="flex items-start justify-between">

          <div>
            <h1
              className="
                text-sm
                font-bold
                tracking-[0.02em]
                text-white
              "
            >
              LIVE ALERTS
            </h1>

            <p
              className="
                mt-0.5
                text-xs
                text-white/65
              "
            >
              (Most Recent First)
            </p>
          </div>

        </div>

        {/* DIVIDER */}
        <div className="mt-1 h-px bg-[#183149]" />

        {/* ALERT LIST */}
        <div
          className="
            mt-1
            h-[calc(100%-58px)]
            overflow-y-auto
            pr-1

            [scrollbar-width:none]
            [&::-webkit-scrollbar]:hidden
          "
        >
          <div className="space-y-px">

            {dynamicAlerts.map((alert, index) => (
              <div
                key={index}
                className="
                  flex
                  items-start
                  gap-2
                  rounded-[8px]
                  border-b
                  border-[#10263b]
                  px-[2px]
                  py-[8px]
                  transition-all
                  duration-200
                  hover:bg-[#0a2034]/50
                "
              >
                {/* STATUS DOT */}
                <div className="pt-[5px]">
                  <div
                    className={`
                      h-[10px]
                      w-[10px]
                      rounded-full
                      shadow-[0_0_10px_rgba(255,255,255,0.25)]

                      ${
                        alert.color === 'green'
                          ? 'bg-[#4ade80]'
                          : alert.color === 'red'
                          ? 'bg-[#ff4d4f]'
                          : alert.color === 'yellow'
                          ? 'bg-[#facc15]'
                          : 'bg-white'
                      }
                    `}
                  />
                </div>

                {/* CONTENT */}
                <div className="min-w-0 flex-1">

                  {/* TOP ROW */}
                  <div className="flex items-center gap-2">

                    {/* TIME */}
                    <span
                      className="
                        shrink-0
                        text-[11px]
                        font-medium
                        text-white/80
                      "
                    >
                      {alert.time}
                    </span>

                    {/* TAG */}
                    <div
                      className="
                        flex
                        h-[22px]
                        min-w-[28px]
                        items-center
                        justify-center
                        rounded-[5px]
                        bg-[#173b63]
                        px-[6px]
                        text-[11px]
                        font-semibold
                        text-white
                      "
                    >
                      {alert.tag}
                    </div>

                    {/* STOCK */}
                    <span
                      className="
                        truncate
                        text-xs
                        font-semibold
                        text-white
                      "
                    >
                      {alert.stock}
                    </span>

                    {/* TIMEFRAME */}
                    {alert.timeframe && (
                      <span
                        className="
                          shrink-0
                          text-[11px]
                          text-white/75
                        "
                      >
                        {alert.timeframe}
                      </span>
                    )}  

                  </div>

                  {/* STATUS */}
                    <span
                      className={`
                        truncate
                        text-[11px]
                        font-semibold

                        ${
                          alert.color === 'green'
                            ? 'text-[#55d86a]'
                            : alert.color === 'red'
                            ? 'text-[#ff5757]'
                            : alert.color === 'yellow'
                            ? 'text-[#facc15]'
                            : 'text-white'
                        }
                      `}
                    >
                      {alert.status}
                    </span>

                  {/* DESCRIPTION */}
                  <p
                    className="
                      mt-1
                      text-[0.76rem]
                      text-white/72
                    "
                  >
                    {alert.description}
                  </p>

                </div>
              </div>
            ))}

          </div>
        </div>

      </div>
    </article>
  )
}

export default LiveAlertsCard
