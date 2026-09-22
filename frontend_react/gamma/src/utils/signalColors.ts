export const getSignalColor = (signal?: string) => {
  const value = signal?.toLowerCase()

  if (value === 'bullish') {
    return 'bg-[#ECFDF3] text-[#027A48] border border-[#ABEFC6]'
  }

  if (value === 'bearish') {
    return 'bg-[#FEF3F2] text-[#B42318] border border-[#FECDCA]'
  }

  return 'bg-[#FFFAEB] text-[#B54708] border border-[#FEC84B]'
}
