import React from 'react'
import S1Card from './S1Card/S1Card'
import LiveAlertsCard from './LiveAlertCard/LiveAlertsCard'

function Overview() {
  return (
    <div
      className="
        grid
        h-[calc(100vh-var(--header-height))]
        gap-2
        grid-cols-1
        min-[768px]:grid-cols-[1fr_270px]
        min-[1400px]:grid-cols-[1fr_300px]
      "
    >
      <div className="min-w-0 flex flex-col gap-2">
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
          <S1Card />
        </div>
      </div>

      <aside
        className="
          hidden
          min-[768px]:block
          rounded-card
          bg-bg-secondary
          overflow-hidden
        "
      >
        <LiveAlertsCard />
      </aside>
    </div>
  )
}

export default Overview;