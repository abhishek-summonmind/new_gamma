import type { HeaderActionKind } from '../headerContent'

type HeaderActionButtonProps = {
  action: {
    kind: HeaderActionKind
    label: string
    badge?: string
    emphasis?: 'default' | 'danger'
  }
}

function ActionIcon({ kind }: { kind: HeaderActionKind }) {
  if (kind === 'alerts') {
    return (
      <svg aria-hidden="true" className="h-[18px] w-[18px]" fill="none" viewBox="0 0 24 24">
        <path
          d="M9 18H15"
          stroke="currentColor"
          strokeLinecap="round"
          strokeWidth="1.7"
        />
        <path
          d="M10 20.5C10.4 21.1 11.1 21.5 12 21.5C12.9 21.5 13.6 21.1 14 20.5"
          stroke="currentColor"
          strokeLinecap="round"
          strokeWidth="1.7"
        />
        <path
          d="M6.5 16.5H17.5C16.4 15.5 15.75 14.15 15.75 12.75V10.75C15.75 8.68 14.07 7 12 7C9.93 7 8.25 8.68 8.25 10.75V12.75C8.25 14.15 7.6 15.5 6.5 16.5Z"
          stroke="currentColor"
          strokeLinejoin="round"
          strokeWidth="1.7"
        />
      </svg>
    )
  }


  return (
    <svg aria-hidden="true" className="h-[18px] w-[18px]" fill="none" viewBox="0 0 24 24">
      <path
        d="M9 8L15 12L9 16"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
      <path
        d="M4.5 5.5H12C15.87 5.5 19 8.63 19 12.5C19 16.37 15.87 19.5 12 19.5H4.5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  )
}

function HeaderActionButton({ action }: HeaderActionButtonProps) {
  const isDanger = action.emphasis === 'danger'
  const buttonStateClassName = isDanger
    ? 'hover:border-red/30 hover:bg-red/10'
    : 'hover:border-blue/30 hover:bg-blue/10'
  const iconStateClassName = isDanger
    ? 'group-hover:text-red'
    : 'group-hover:text-text-primary'

  return (
    <button
      aria-label={action.label}
      className={`group relative flex min-h-[76px] flex-col items-center justify-center rounded-[14px] border border-white/[0.06] bg-black/10 px-2 py-2 transition-all duration-200 ${buttonStateClassName}`}
      type="button"
    >
      {action.badge ? (
        <span className="absolute right-2 top-2 min-w-[1.25rem] rounded-full bg-red px-1.5 py-0.5 text-[0.62rem] font-semibold leading-none text-text-primary">
          {action.badge}
        </span>
      ) : null}

      <span
        className={`flex h-9 w-9 items-center justify-center rounded-full border border-white/[0.08] bg-bg-card/80 text-text-secondary transition-colors duration-200 ${iconStateClassName}`}
      >
        <span className="h-[18px] w-[18px]">
          <ActionIcon kind={action.kind} />
        </span>
      </span>

      <span
        className={`mt-1.5 text-center text-[0.7rem] font-medium leading-4 ${isDanger ? 'text-red' : 'text-text-secondary'} transition-colors duration-200 ${iconStateClassName}`}
      >
        {action.label}
      </span>
    </button>
  )
}

export default HeaderActionButton
