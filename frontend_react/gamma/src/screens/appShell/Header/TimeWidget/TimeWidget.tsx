import { useEffect, useMemo, useState } from 'react'

function TimeWidget() {

  const [currentTime, setCurrentTime] =
    useState(new Date())

  useEffect(() => {

    const interval = setInterval(() => {
      setCurrentTime(new Date())
    }, 1000)

    return () => clearInterval(interval)

  }, [])

  const time = useMemo(() => {

    return currentTime.toLocaleTimeString(
      'en-IN',
      {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      },
    )

  }, [currentTime])

  const dateLabel = useMemo(() => {

    return currentTime.toLocaleDateString(
      'en-IN',
      {
        timeZone: 'Asia/Kolkata',
        weekday: 'short',
        day: '2-digit',
        month: 'short',
        year: 'numeric',
      },
    )

  }, [currentTime])

  return (
    <div
      className="
        flex
        h-full
        flex-col
        items-start
        lg:items-center
        justify-center
      "
    >

      <p
        className="
          text-[0.60rem]
          uppercase
          tracking-[0.24em]
          text-text-muted
        "
      >
        Time (IST)
      </p>

      <p
        className="
          mt-1
          text-md
          font-bold
          leading-none
          text-text-primary
          tabular-nums
          sm:text-lg
        "
      >
        {time}
      </p>

      <p
        className="
          mt-1
          text-xs
          text-text-secondary
        "
      >
        {dateLabel}
      </p>

    </div>
  )
}

export default TimeWidget