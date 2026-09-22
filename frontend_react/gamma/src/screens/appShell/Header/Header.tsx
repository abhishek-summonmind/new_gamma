import ActionsSection from './ActionsSection/ActionsSection'
import BrandSection from './BrandSection/BrandSection'
import MarketStats from './MarketStats/MarketStats'
import NextCandle from './NextCandle/NextCandle'


function Header() {
  return (

    <header className="w-full">

      <div
        className="
          grid
          grid-cols-1
          gap-3

          sm:grid-cols-2

          lg:grid-cols-[1fr_1.3fr_120px_280px]

          items-stretch
        "
      >


        {/* BRAND */}
        <div
          className="
            min-w-0
            flex
            items-center
          "
        >
          <BrandSection />
        </div>



        {/* MARKET */}
        {/* <div
          className="
            min-w-0
            flex
            items-center
          "
        >
          <MarketStats />
        </div> */}

        {/* NEXT CANDLE */}
        <div
          className="
            min-w-0
            flex
            items-center
            justify-center
          "
        >
          <NextCandle />

        </div>





        {/* ACTIONS */}
        <div
          className="
            min-w-0
            flex
            items-center
            justify-end
          "
        >

          <ActionsSection />

        </div>


      </div>


    </header>

  )

}


export default Header
