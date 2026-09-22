import { AnimatePresence, motion } from 'framer-motion'
import { useState } from 'react'
import S9Screen from '../s9/S9Screen'

type ScreenTabId = 's9' 

type ScreenTab = {
  id: ScreenTabId
  label: string
  eyebrow: string
  title: string
  description: string
  tags: string[]
  stats: Array<{
    label: string
    value: string
  }>
  accentClassName: string
  badgeClassName: string
}

const screenTabs: ScreenTab[] = [
  {
    id: 's9',
    label: 'S9',
    eyebrow: 'Market-wide options',
    title: 'S9 Top Opportunities',
    description: 'Ranks configured stocks and indices by option-chain filter score.',
    tags: ['Options', 'S9', 'Top Matches'],
    stats: [
      { label: 'Focus', value: 'Option signals' },
      { label: 'Bias', value: 'Aligned trades' },
      { label: 'Speed', value: 'Live scan' },
    ],
    accentClassName: 'from-indigo-500/18 via-violet-500/10 to-transparent',
    badgeClassName: 'bg-indigo-500/15 text-indigo-200 ring-indigo/400/20',
  },
]

function ScreenSwitch() {
  const [activeTab, setActiveTab] = useState<ScreenTabId>('s9')

  const activeScreen = screenTabs.find((tab) => tab.id === activeTab) ?? screenTabs[0]

  const renderScreen = () => {
    switch (activeTab) {
      case 's9':
        return <S9Screen />
      default:
        return <S9Screen />
    }
  }

  return (
    <section className="mt-1 overflow-hidden">
      {/* <div className="bg-bg-card">
        <div className="overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <div
            aria-label="Screen switch tabs"
            className="flex min-w-max items-center gap-1"
            role="tablist"
          >
            {screenTabs.map((tab) => {
              const isActive = tab.id === activeScreen.id

              return (
                <button
                  key={tab.id}
                  aria-controls={`screen-panel-${tab.id}`}
                  aria-selected={isActive}
                  className="relative isolate shrink-0 overflow-hidden rounded-[12px] px-3 py-2 text-[0.76rem] font-medium tracking-[0.01em] transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue/45"
                  id={`screen-tab-${tab.id}`}
                  onClick={() => setActiveTab(tab.id)}
                  role="tab"
                  type="button"
                >
                  {isActive ? (
                    <motion.span
                      layoutId="screen-switch-active-pill"
                      className="absolute inset-0 rounded-[12px] border border-blue/25 bg-blue/14 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_10px_24px_rgba(59,130,246,0.16)]"
                      transition={{ bounce: 0.18, duration: 0.35 }}
                    />
                  ) : null}

                  <span
                    className={`relative z-10 whitespace-nowrap ${isActive ? 'text-text-primary' : 'text-text-secondary hover:text-text-primary'}`}
                  >
                    {tab.label}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      </div> */}

      <AnimatePresence mode="wait">
        <motion.div
          key={activeScreen.id}
          animate={{ opacity: 1, y: 0 }}
          aria-labelledby={`screen-tab-${activeScreen.id}`}
          className="mt-3"
          id={`screen-panel-${activeScreen.id}`}
          initial={{ opacity: 0, y: 10 }}
          role="tabpanel"
          transition={{ duration: 0.24, ease: 'easeOut' }}
        >
          {renderScreen()}
        </motion.div>
      </AnimatePresence>
    </section>
  )
}

export default ScreenSwitch
