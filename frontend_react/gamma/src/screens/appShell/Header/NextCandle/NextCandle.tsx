// import { CandleData } from '../headerContent'
import { useEffect, useMemo, useState } from 'react'

export const CandleData = {
    label: 'Next Candle',
    value: '5m',
    detail: '00:00:24',
    tone: 'blue',
  }

function NextCandle() {

  const [timeLeft, setTimeLeft] =
    useState('00:05:59')

  useEffect(() => {

    const updateCountdown = () => {

      const now = new Date()

      const minutes = now.getMinutes()
      const seconds = now.getSeconds()

      // current 5 minute bucket
      const currentBlockMinute =
        Math.floor(minutes / 5) * 5

      // next 5 minute mark
      const nextBlock = new Date(now)

      nextBlock.setMinutes(
        currentBlockMinute + 5,
      )

      nextBlock.setSeconds(0)
      nextBlock.setMilliseconds(0)

      const diff =
        nextBlock.getTime() - now.getTime()
      
      const totalSeconds =
        Math.floor(diff / 1000)

      const mins = Math.floor(
        totalSeconds / 60,
      )

      const secs = totalSeconds % 60

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
    <div className="flex h-full flex-col justify-center items-start
        lg:items-center">

            <p className="text-[0.60rem] uppercase tracking-[0.24em] font-bold text-gray-900">
              Next Candle
            </p>

              {/* <span className={`h-2 w-2 rounded-full bg-green`} /> */}
              <span className={`mt-1 text-md font-bold leading-none text-black tabular-nums sm:text-lg`}>
                5m
              </span>

            <p className="mt-1 text-sm font-semibold text-cyan-900">
              {timeLeft}
            </p>
    </div>
  )
}

export default NextCandle