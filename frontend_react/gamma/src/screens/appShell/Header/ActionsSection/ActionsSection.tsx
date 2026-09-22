import React, { useState } from 'react'
import { useMinuteStore } from '../../../../stores/minuteStore';
import { RefreshCcw, Settings } from 'lucide-react';
import { useRefresh } from '../../../../features/hooks/useRefresh';
import { useQueryClient } from '@tanstack/react-query';
import { useRefreshStore } from '../../../../stores/refreshStore';
import DhanConfigModal from '../../../../components/DhanConfigModal';
import { getSelectedIndexSymbol } from '../../../../services/indexSelection';



function ActionsSection() {
  // const [stateMinute, setStateMinute] = useState('5m')
  const [isOpen, setIsOpen] = useState(false)
  const [isDhanConfigOpen, setIsDhanConfigOpen] = useState(false)
  const setMinute = useMinuteStore((state) => state.setMinute);
  const minute = useMinuteStore((state) => state.minute);
  const options = ['5m', '10m', '15m']
  const isRefreshing =
    useRefreshStore(
      (state) => state.isRefreshing
    )

  const setIsRefreshing =
    useRefreshStore(
      (state) => state.setIsRefreshing
    )

const queryClient = useQueryClient()

  const {
    mutateAsync: refreshData,
  } = useRefresh()

  const handleRefresh = async () => {

    if(isRefreshing) return;

    try {

      setIsRefreshing(true)
      console.log("STEP 1 → refresh backend")

      // refresh API
      await refreshData(getSelectedIndexSymbol())

      console.log("STEP 2 → wait backend sync")

      // optional but recommended
      // await new Promise((resolve) =>
      //   setTimeout(resolve, 3000)
      // )

      console.log("STEP 3 → refetch all APIs")

      // all mounted queries auto refetch
      await queryClient.invalidateQueries({
        refetchType: "active",
      })

      console.log("REFRESH SUCCESS")

    } catch (error) {
      console.log("STEP 3 → refetch all APIs")
      await queryClient.invalidateQueries()
      console.log("REFRESH FAILED:", error)
    }
    finally {
    setIsRefreshing(false)
  }  

  }

  const handleSelect = (value: string) => {
    setMinute(value)
    setIsOpen(false)
  }

  // const handleSelect = (value: string) => {
  //   setMinute(value)
  //   setIsOpen(false)
  // }

  return (
    <div className="flex h-full items-center justify-start gap-2">

      <button
  onClick={handleRefresh}

  disabled={isRefreshing}

  className="
    flex
    flex-row
    items-center
    justify-between
    gap-2
    rounded-[10px]
    border
    bg-gray-100
    px-2
    py-2
    transition-all
    duration-200
    border-gray-200
hover:border-gray-300
    disabled:opacity-50
    disabled:cursor-not-allowed
  "
>

  <RefreshCcw
    width={12}
    height={12}
    className={
      isRefreshing
        ? "animate-spin"
        : ""
    }
    color='black'
  />

  <span className="text-sm text-black">

    {isRefreshing
      ? "Refreshing..."
      : "Refresh"}

  </span>

</button>


      {/* MINUTE DROPDOWN */}
      <div className="relative">

        {/* SELECT BUTTON */}
        <button
          onClick={() => setIsOpen((prev) => !prev)}
          className="
            flex
            h-[52px]
            min-w-[88px]
            items-center
            justify-between
            rounded-[10px]
            border
            border-gray-200
hover:border-gray-300
            bg-gray-100
            px-3
            transition-all
            duration-200
          "
        >
          <div className="flex flex-col items-start">
            <span className="text-[10px] font-medium text-gray-900 uppercase tracking-[0.08em] ">
              Interval
            </span>

            <span className="text-[15px] font-semibold text-black">
              {minute}
            </span>
          </div>

          {/* ARROW */}
          <svg
            className={`
              h-4
              w-4
              text-gray-900
              transition-transform
              duration-200

              ${isOpen ? 'rotate-180' : ''}
            `}
            fill="none"
            viewBox="0 0 24 24"
          >
            <path
              d="M6 9L12 15L18 9"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>

        {/* DROPDOWN */}
        {isOpen && (
          <div
            className="
              absolute
              right-0
              top-[58px]
              z-50
              w-full
              overflow-hidden
              rounded-[10px]
              border
              border-gray-200
hover:border-gray-300
              bg-gray-100
            "
          >
            {options.map((item) => (
              <button
                key={item}
                onClick={() => handleSelect(item)}
                className={`
                  flex
                  w-full
                  items-center
                  justify-between
                  px-3
                  py-2.5
                  text-left
                  text-sm
                  font-medium
                  transition-all
                  duration-150

                  ${
                    minute === item
                      ? 'bg-blue-500/10 text-blue'
                      : 'text-gray-900 hover:bg-gray-200'
                  }
                `}
              >
                {item}

                {minute === item && (
                  <svg
                    className="h-4 w-4"
                    fill="none"
                    viewBox="0 0 24 24"
                  >
                    <path
                      d="M5 13L9 17L19 7"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* BROKER CONFIG BUTTON */}
      <button
        onClick={() => setIsDhanConfigOpen(true)}
        className="
          flex
          flex-row
          items-center
          justify-center
          gap-2
          rounded-[10px]
          border
          border-gray-200
hover:border-gray-300

          bg-gray-100
          px-3
          py-2
          transition-all
          duration-200
        "
        title="Configure broker credentials and option expiry"
      >
        <Settings
          width={16}
          height={16}
          color='black'
        />
        {/* <span className="text-sm">Settings</span> */}
      </button>

      {/* BROKER CONFIG MODAL */}
      <DhanConfigModal
        isOpen={isDhanConfigOpen}
        onClose={() => setIsDhanConfigOpen(false)}
      />

      {/* ALERT BUTTON */}
    </div>
  )
}

export default ActionsSection
      //     "
      //   >
      //     Alerts
      //   </span>
      // </button>
