type Props = {
  value: number;
};
const timeframeSignals = [
  { timeframe: "5m", bullish: 6, bearish: 4 },
  { timeframe: "10m", bullish: 5, bearish: 3 },
  { timeframe: "15m", bullish: 4, bearish: 2 },
];

function ConvictionScore({ value }: Props) {
  const stroke = 18;

  return (
    <article className="relative min-h-[194px] w-full overflow-hidden rounded-[12px] border border-[#17314d] bg-[linear-gradient(180deg,#0d1d31_0%,#091728_100%)] px-4 py-3 shadow-[0_22px_60px_rgba(2,10,20,0.32)]">
      {/* <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(59,130,246,0.14),transparent_40%)]" /> */}
      <div className="h-full flex flex-col items-center">
        <div className=" flex flex-row gap-2">
          <span className="text-xs font-semibold leading-none text-white">
            MARKET BIAS
          </span>
          <span className="text-xs leading-none text-white">
            (All Screeners)
          </span>
        </div>
        <div
  className="
    relative
    mt-1
    flex
    h-full
    w-full
    items-center
    justify-center
  "
>
          <svg className="w-50 h-30" viewBox="0 0 220 120">
            {/* Background Arc */}
            <path
              d="
                M 20 110
                A 90 90 0 0 1 200 110
                "
              fill="none"
              stroke="#1f2d45"
              strokeWidth={stroke}
              strokeLinecap="round"
            />

            {/* Progress Arc */}
            <path
  d="
    M 20 110
    A 90 90 0 0 1 200 110
  "
  fill="none"
  stroke="url(#gradient)"
  strokeWidth={stroke}
  strokeLinecap="round"
  pathLength={100}
  strokeDasharray="100"
  strokeDashoffset={100 - value}
  className="transition-all duration-700 ease-out"
/>

            <defs>
              <linearGradient id="gradient">
                <stop offset="0%" stopColor="#22c55e" />
                <stop offset="50%" stopColor="#eab308" />
                <stop offset="100%" stopColor="#ef4444" />
              </linearGradient>
            </defs>
          </svg>

          {/* Center Content */}
          <div
  className="
    absolute
    top-[58%]
    left-1/2
    flex
    -translate-x-1/2
    -translate-y-1/4
    flex-col
    items-center
  "
>

  <h1
    className="
      text-3xl
      font-bold
      leading-none
      text-text-primary
    "
  >
    {value}%
  </h1>

  <p
    className="
      mt-1
      text-[15px]
      font-semibold
      text-[#56d96c]
    "
  >
    BULLISH
  </p>

  {/* <span
    className="
      mt-0.5
      text-sm
      text-text-secondary
    "
  >
    Moderate Conviction
  </span> */}

</div>
        </div>
      </div>
    </article>
  );
}

export default ConvictionScore;
