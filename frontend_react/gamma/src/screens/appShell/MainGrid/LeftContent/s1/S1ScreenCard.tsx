import { useMemo } from "react"
import TimeRange from "../../../../../components/TimeRange"
import { useMinuteStore } from "../../../../../stores/minuteStore"
import { useS1 } from "../../../../../features/hooks/useS1"
import { calculateSignals } from "../../../../../utils/calculateSignals"

function S1ScreenCard() {

  const { minute } = useMinuteStore()

  const {
    data,
    isLoading,
  } = useS1()

  const signalData = useMemo(
    () =>
      calculateSignals(
        data?.items || [],
      ),
    [data],
  )

  const timeframeMap = useMemo(() => {

    return (
      signalData.timeframeSignals.reduce(
        (acc, item) => {

          acc[item.timeframe] = item

          return acc

        },
        {} as Record<
          string,
          {
            bullish: number
            bearish: number
            neutral: number
          }
        >,
      )
    )

  }, [signalData])

  const getTotal = (
    timeframe: '5m' | '10m' | '15m',
  ) => {

    return (
      (timeframeMap[timeframe]
        ?.bullish || 0)
      +
      (timeframeMap[timeframe]
        ?.bearish || 0)
      +
      (timeframeMap[timeframe]
        ?.neutral || 0)
    )

  }

  return (
    <article
      className="
        relative
        overflow-hidden
        rounded-[8px]
        border
        border-[#162841]
        bg-[#071224]
      "
    >

      {/* TOP SECTION */}
      <div
        className="
          flex
          items-center
          justify-between
          border-b
          border-[#13233a]
          px-3
          py-2
        "
      >

        <div>

          <h2
            className="
              text-[11px]
              font-semibold
              uppercase
              tracking-[0.06em]
              text-white/90
            "
          >
            SUMMARY
            <span
              className="
                ml-1
                text-white/40
              "
            >
              - COUNT (OF 50 STOCKS)
              PER CATEGORY
            </span>
          </h2>

        </div>

        

      </div>

      {/* TABLE */}
      <div className="overflow-x-auto">

        {/* TABLE HEADER */}
        <div
          className="
            grid
            min-w-[680px]
            grid-cols-[2.5fr_1fr_1fr_1fr]
            border-b
            border-[#13233a]
            bg-[#081426]
            px-3
            py-2
          "
        >

          <div
            className="
              text-[11px]
              font-medium
              text-white/55
            "
          >
            Category
          </div>

          <div
            className="
              text-center
              text-[11px]
              font-medium
              text-white/55
            "
          >
            5m
          </div>

          <div
            className="
              text-center
              text-[11px]
              font-medium
              text-white/55
            "
          >
            10m
          </div>

          <div
            className="
              text-center
              text-[11px]
              font-medium
              text-white/55
            "
          >
            15m
          </div>

        </div>

        {/* ROWS */}
        <div>

          {/* BULLISH */}
          <div
            className="
              grid
              min-w-[680px]
              grid-cols-[2.5fr_1fr_1fr_1fr]
              items-center
              border-b
              border-[#112136]
              px-3
              py-3
            "
          >

            <div
              className="
                flex
                items-center
                gap-2
              "
            >

              <div
                className="
                  h-[6px]
                  w-[6px]
                  rounded-full
                  bg-[#56d96c]
                "
              />

              <div
                className="
                  text-[11px]
                  font-medium
                  text-[#d8e4f7]
                "
              >
                Bullish
                <span
                  className="
                    ml-1
                    text-white/35
                  "
                >
                  (McGinley up +
                  RSI &gt; 55+
                  MACD up)
                </span>
              </div>

            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['5m']
                  ?.bullish || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['10m']
                  ?.bullish || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['15m']
                  ?.bullish || 0
              }
            </div>

          </div>

          {/* BEARISH */}
          <div
            className="
              grid
              min-w-[680px]
              grid-cols-[2.5fr_1fr_1fr_1fr]
              items-center
              border-b
              border-[#112136]
              px-3
              py-3
            "
          >

            <div
              className="
                flex
                items-center
                gap-2
              "
            >

              <div
                className="
                  h-[6px]
                  w-[6px]
                  rounded-full
                  bg-[#ff5757]
                "
              />

              <div
                className="
                  text-[11px]
                  font-medium
                  text-[#d8e4f7]
                "
              >
                Bearish
                <span
                  className="
                    ml-1
                    text-white/35
                  "
                >
                  (McGinley down +
                  RSI &lt; 45 +
                  MACD down)
                </span>
              </div>

            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['5m']
                  ?.bearish || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['10m']
                  ?.bearish || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['15m']
                  ?.bearish || 0
              }
            </div>

          </div>

          {/* NEUTRAL */}
          <div
            className="
              grid
              min-w-[680px]
              grid-cols-[2.5fr_1fr_1fr_1fr]
              items-center
              border-b
              border-[#112136]
              px-3
              py-3
            "
          >

            <div
              className="
                flex
                items-center
                gap-2
              "
            >

              <div
                className="
                  h-[6px]
                  w-[6px]
                  rounded-full
                  bg-[#7c8799]
                "
              />

              <div
                className="
                  text-[11px]
                  font-medium
                  text-[#d8e4f7]
                "
              >
                Neutral
                <span
                  className="
                    ml-1
                    text-white/35
                  "
                >
                  / cascade halted on
                  a gate
                </span>
              </div>

            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['5m']
                  ?.neutral || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['10m']
                  ?.neutral || 0
              }
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-semibold
                text-white
              "
            >
              {
                timeframeMap['15m']
                  ?.neutral || 0
              }
            </div>

          </div>

          {/* TOTAL */}
          <div
            className="
              grid
              min-w-[680px]
              grid-cols-[2.5fr_1fr_1fr_1fr]
              items-center
              bg-[#081426]
              px-3
              py-3
            "
          >

            <div
              className="
                text-[12px]
                font-semibold
                text-white
              "
            >
              Total
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-bold
                text-white
              "
            >
              {getTotal('5m')}
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-bold
                text-white
              "
            >
              {getTotal('10m')}
            </div>

            <div
              className="
                text-center
                text-[12px]
                font-bold
                text-white
              "
            >
              {getTotal('15m')}
            </div>

          </div>

        </div>

      </div>

    </article>
  )
}

export default S1ScreenCard