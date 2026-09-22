import { useState } from "react"

type SignalItem = {
  confidence: number | null
  created_at: string
  id: number
  payload?: {
    signal_time?: string | null
    signals?: {
      '5m'?: string | null
      '10m'?: string | null
      '15m'?: string | null
    }
  }
  reason?: string | null
  signal?: string | null
  symbol?: string | null
}

type Props = {
  items: SignalItem[]
}

function SignalsTable({ items }: Props) {

  const [sortOrder, setSortOrder] =
  useState<
    'none' | 'asc' | 'desc'
  >('none')

  const getBadgeStyle = (value?: string | null) => {
    if (!value) {
      return 'bg-slate-500/15 text-slate-300 border border-slate-500/20'
    }

    const signal = value.toLowerCase()

    if (
      signal.includes('buy') ||
      signal.includes('bullish')
    ) {
      return `
        bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6]
      `
    }

    if (
      signal.includes('sell') ||
      signal.includes('bearish')
    ) {
      return `
        bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]
      `
    }

    if (
      signal.includes('neutral')
    ) {
      return `
        bg-[#FFFAEB] text-[#B54708] border border-[#FEC84B]
      `
    }

    if (
      signal.includes('watch')
    ) {
      return `
        bg-[#EFF8FF] text-[#175CD3] border border-[#B2DDFF]
      `
    }

    return `
      bg-[#F2F4F7] text-[#475467] border border-[#D0D5DD]
    `
  }

  const formatValue = (
    value?: string | number | null
  ) => {
    if (
      value === null ||
      value === undefined ||
      value === ''
    ) {
      return 'Undefined'
    }

    return value
  }

  return (
    <div
      className="
        relative
        overflow-hidden
        rounded-2xl
        border
        border-gray-200
        bg-gray-50
      "
    >
      {/* TOP GLOW */}
      <div className="absolute inset-x-0 top-0 h-px bg-gray-50" />

      {/* GRID TEXTURE */}
     

      <div className="relative z-10">

        {/* HEADER */}
        <div
          className="
            flex
            items-center
            justify-between
            border-b
            border-[#17314d]
            px-5
            py-4
          "
        >
          <div>
            <h2
              className="
                text-[1rem] font-bold text-black uppercase
              "
            >
              Live Signals
            </h2>

          </div>
        </div>

        {/* TABLE */}
        <div className="overflow-x-auto">

          <table className="w-full min-w-[1200px] border-collapse">

            {/* TABLE HEAD */}
            <thead>
              <tr
                className="
                  border-b
                  border-gray-200
                  bg-gray-50
                "
              >
                {[
                  'Symbol',
                  'Signal',
                  '5m',
                  '10m',
                  '15m',
                  'Signal Time',
                  'Confidence',
                  'Reason',
                ].map((heading) => (
                  <th
                    key={heading}
                    className="
                      whitespace-nowrap
                      px-4
                      py-3
                      text-left
                      text-[0.7rem]
                      font-semibold
                      uppercase
                      tracking-[0.08em]
                      text-gray-900
                    "
                  >
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>

            {/* TABLE BODY */}
            <tbody>
  {[...items]

    .sort((a, b) => {

      if (sortOrder === 'asc') {

        return (
          (a.symbol || '')
            .localeCompare(
              b.symbol || '',
            )
        )

      }

      if (sortOrder === 'desc') {

        return (
          (b.symbol || '')
            .localeCompare(
              a.symbol || '',
            )
        )

      }

      return 0

    })

    .map((item, index) => {

                const signals =
                  item.payload?.signals

                return (
                  <tr
                    key={item.id}
                    className={`
                      border-b
                      border-gray-200
                      transition-all
                      duration-200

                      ${
                        index % 2 === 0
                          ? 'bg-gray-50'
                          : 'bg-gray-50'
                      }
                    `}
                  >
                    {/* SYMBOL */}
                    <td
                      className="
                        whitespace-nowrap
                        px-4
                        py-4
                        text-black
                      "
                    >
                      <div
                        className="
                          font-semibold
                          text-sm
                          tracking-[0.02em]
                          
                        "
                      >
                        {formatValue(item.symbol)}
                      </div>
                    </td>

                    {/* SIGNAL */}
                    <td className="px-4 py-4">
                      <span
                        className={`
                          inline-flex
                          items-center
                          rounded-full
                          px-3
                          py-1
                          text-[0.7rem]
                          font-semibold
                          capitalize
                          ${getBadgeStyle(item.signal)}
                        `}
                      >
                        {formatValue(item.signal)}
                      </span>
                    </td>

                    {/* 5M */}
                    <td className="px-4 py-4">
                      <span
                        className={`
                          inline-flex
                          items-center
                          rounded-full
                          px-3
                          py-1
                          text-[0.7rem]
                          font-semibold
                          capitalize
                          ${getBadgeStyle(signals?.['5m'])}
                        `}
                      >
                        {formatValue(signals?.['5m'])}
                      </span>
                    </td>

                    {/* 10M */}
                    <td className="px-4 py-4">
                      <span
                        className={`
                          inline-flex
                          items-center
                          rounded-full
                          px-3
                          py-1
                          text-[0.7rem]
                          font-semibold
                          capitalize
                          ${getBadgeStyle(signals?.['10m'])}
                        `}
                      >
                        {formatValue(signals?.['10m'])}
                      </span>
                    </td>

                    {/* 15M */}
                    <td className="px-4 py-4">
                      <span
                        className={`
                          inline-flex
                          items-center
                          rounded-full
                          px-3
                          py-1
                          text-[0.7rem]
                          font-semibold
                          capitalize
                          ${getBadgeStyle(signals?.['15m'])}
                        `}
                      >
                        {formatValue(signals?.['15m'])}
                      </span>
                    </td>

                    {/* SIGNAL TIME */}
                    <td
                      className="
                        whitespace-nowrap
                        px-4
                        py-4
                        text-sm
                        text-gray-900
                      "
                    >
                      {/* {formatValue(
                        item.payload?.signal_time
                      )} */}
                      {
  item.payload?.signal_time
    ? new Date(
        item.payload?.signal_time
      ).toLocaleTimeString(
        'en-IN',
        {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
        },
      )
    : 'Undefined'
}
                    </td>

                    {/* CONFIDENCE */}
                    <td className="px-4 py-4">
                      <div
                        className="
                          flex
                          items-center
                          justify-center
                        "
                      >
                        {/* <div
                          className="
                            h-2
                            w-full
                            max-w-[90px]
                            overflow-hidden
                            rounded-full
                            bg-white/10
                          "
                        >
                          <div
                            className="
                              h-full
                              rounded-full
                              bg-[linear-gradient(90deg,#06b6d4_0%,#3b82f6_100%)]
                            "
                            style={{
                              width: `${
                                (item.confidence || 0) * 100
                              }%`,
                            }}
                          />
                        </div> */}

                        <span
                          className="
                            text-xs
                            font-semibold
                            text-cyan-900
                          "
                        >
                            {item.confidence}
                          {/* {item.confidence
                            ? `${(
                                item.confidence * 100
                              ).toFixed(1)}%`
                            : 'Undefined'} */}
                        </span>
                      </div>
                    </td>

                    {/* REASON */}
                    <td
                      className="
                        min-w-[320px]
                        px-4
                        py-4
                        text-sm
                        leading-6
                        text-gray-900
                      "
                    >
                      {formatValue(item.reason)}
                    </td>
                  </tr>
                )
              })}
            </tbody>

          </table>
        </div>
      </div>
    </div>
  )
}

export default SignalsTable