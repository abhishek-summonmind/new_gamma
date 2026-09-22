import React, { useMemo } from 'react'
import SignalsTable from '../../../../../components/SignalsTable'
import { useS1 } from '../../../../../features/hooks/useS1'
import S1Card from '../Overview/S1Card/S1Card'
import S1ScreenCard from './S1ScreenCard'
import { getSelectedIndexSymbol, isSupportedIndexSymbol, normalizeIndexSymbol } from '../../../../../services/indexSelection'

type S1Item = {
  symbol?: string | null
}

function S1Screen() {

  const { data, isLoading } = useS1()

//   const s1Data = data?.items || []
  const s1Data = useMemo(
  () => (data?.items || []).filter((item: S1Item) => isSupportedIndexSymbol(item?.symbol) && normalizeIndexSymbol(item?.symbol) === getSelectedIndexSymbol()),
  [data]
)

//   if (isLoading) {
//     return (
//       <div className="text-white">
//         Loading...
//       </div>
//     )
//   }

  return (
    <div className='flex flex-col gap-5' >
    {/* <S1ScreenCard/> */}
    <SignalsTable items={s1Data} />
  </div>
  )
}

export default S1Screen
