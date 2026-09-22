import TimeRange from "../../../../../../components/TimeRange";
import { useS1 } from "../../../../../../features/hooks/useS1"
import { useS1Signals } from "../../../../../../features/hooks/useS1Singnals";
import { getS1 } from "../../../../../../services/S1Api"
import { useMinuteStore } from "../../../../../../stores/minuteStore";
import { calculateSignals } from "../../../../../../utils/calculateSignals";

const timeframeSignals = [
  { timeframe: '5m', bullish: 6, bearish: 4 },
  { timeframe: '10m', bullish: 5, bearish: 3 },
  { timeframe: '15m', bullish: 4, bearish: 2 },
]

function S1TrendIcon() {
  

  return (
    <span className="flex h-7 w-7 items-center justify-center rounded-[8px] border border-[#224a74] bg-[linear-gradient(180deg,#14365a_0%,#0c2137_100%)] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
      <svg
        aria-hidden="true"
        className="h-[18px] w-[18px]"
        fill="none"
        viewBox="0 0 24 24"
      >
        <path
          d="M4.5 18.25V10.5M9.5 18.25V6.75M14.5 18.25V12.25M19.5 18.25V4.75"
          stroke="#6AB7FF"
          strokeLinecap="round"
          strokeWidth="1.85"
        />
        <path
          d="M4.75 14.5 9.1 10.3l3.2 2.15 6.95-6.6"
          stroke="#9BD3FF"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.85"
        />
      </svg>
    </span>
  )
}

function S1Card() {
  const {minute} = useMinuteStore();
  const {data , isLoading } = useS1();
  console.log("s1 data: ",data);
  const signalData = calculateSignals(data?.items || []);

  const totalBullish = signalData.totals.bullish;
  const totalBearish = signalData.totals.bearish;
  
  return (
    <article className="relative  w-full overflow-hidden rounded-[12px] border border-[#17314d] bg-[linear-gradient(180deg,#0d1d31_0%,#091728_100%)] px-4 py-2 shadow-[0_22px_60px_rgba(2,10,20,0.32)]">
      {/* <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(59,130,246,0.14),transparent_40%)]" /> */}

      <div className="relative z-10">
        <div className="flex flex-col items-start justify-between gap-0.5">
          <div className="flex gap-2.5">
            <S1TrendIcon />

            <div className="space-y-0.5">
              <div className="flex flex-row gap-2">
                <span className="text-xs font-semibold leading-none text-white">
                  S1
                </span>
                <span className="text-[11px] leading-none text-white">
                  Triple Confirmation 
                </span>
              </div>

              <p className=" mt-1 text-[11px] leading-none text-[#dde7f7]">
                Cascade (Stocks)
              </p>
            </div>
          </div>

          <p className="self-end text-right text-[10px] font-medium text-[#dbe5f3]">
            <TimeRange minute={minute} />
          </p>
        </div>

        <div className="mt-0.5 grid grid-cols-[1fr_auto_1fr] gap-3">
          <div className="space-y-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs  font-semibold uppercase tracking-[0.02em] text-[#80e160]">
                Bullish
              </span>
              <span className="text-xs font-semibold leading-none text-[#80e160]">
                {signalData.totals.bullish}
              </span>
            </div>

            <div className="space-y-1">
              {signalData.timeframeSignals.map((signal) => (
                <div
                  key={signal.timeframe}
                  className="flex items-center justify-between gap-3 text-xs"
                >
                  <span className="text-xs text-[#f4f7ff]">{signal.timeframe}</span>
                  <span className="text-xs font-medium text-[#80e160]">
                    {signal.bullish}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-1 h-full w-px bg-[#18324f]" />

          <div className="space-y-1">
            <div className="flex items-baseline justify-end gap-2">
              <span className="text-xs font-semibold uppercase tracking-[0.02em] text-[#ff5e5c]">
                Bearish
              </span>
              <span className="text-xs font-semibold leading-none text-[#ff5e5c]">
                {signalData.totals.bearish}
              </span>
            </div>

            <div className="space-y-1">
              {signalData.timeframeSignals.map((signal) => (
                <div
                  key={signal.timeframe}
                  className="text-xs flex items-center justify-end text-[0.84rem]"
                >
                  <span className="text-xs font-medium text-[#ff5e5c]">
                    {signal.bearish}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className=" flex items-center justify-center gap-2 border-t border-[#14273f] pt-2">
          <span className="text-xs font-medium text-[#f4f7ff]">
            Total Signals
          </span>
          <span className="text-xs font-semibold text-white">{signalData.totals.bullish + signalData.totals.bearish}</span>
        </div>
      </div>
    </article>
  )
}

export default S1Card
