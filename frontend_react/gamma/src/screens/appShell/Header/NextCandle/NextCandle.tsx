import { useEffect, useState } from 'react'

export const CandleData = {
  label: 'Next Candle',
  value: '3m',
  detail: '00:00:24',
  tone: 'blue',
}

function NextCandle() {
  const [timeLeft, setTimeLeft] = useState('00:02:59')

  useEffect(() => {
    const updateCountdown = () => {
      const now = new Date()

      const minutes = now.getMinutes()

      // Current 3-minute bucket
      const currentBlockMinute =
        Math.floor(minutes / 3) * 3

      // Next 3-minute candle
      const nextBlock = new Date(now)

      nextBlock.setMinutes(
        currentBlockMinute + 3,
      )

      nextBlock.setSeconds(0)
      nextBlock.setMilliseconds(0)

      const diff =
        nextBlock.getTime() - now.getTime()

      const totalSeconds =
        Math.max(0, Math.floor(diff / 1000))

      const mins =
        Math.floor(totalSeconds / 60)

      const secs =
        totalSeconds % 60

      const formatted =
        `00:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`

      setTimeLeft(formatted)
    }

    updateCountdown()

    const interval = setInterval(
      updateCountdown,
      1000,
    )

    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex h-full flex-col justify-center items-start lg:items-center">
      <p className="text-[0.60rem] uppercase tracking-[0.24em] font-bold text-gray-900">
        Next Candle
      </p>

      <span className="mt-1 text-md font-bold leading-none text-black tabular-nums sm:text-lg">
        3m
      </span>

      <p className="mt-1 text-sm font-semibold text-cyan-900">
        {timeLeft}
      </p>
    </div>
  )
}

export default NextCandle