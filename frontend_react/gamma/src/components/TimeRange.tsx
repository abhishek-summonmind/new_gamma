interface TimeRangeProps {
  minute: string
}

function TimeRange({ minute }: TimeRangeProps) {
  return (
    <span className="text-white/70">
      ({minute}m window)
    </span>
  )
}

export default TimeRange