import { useAutoRefresh } from '../features/hooks/useAutoRefresh'
import { useCandleRefresh } from '../features/hooks/useCandleRefresh'
import { useDashboardData } from '../features/hooks/useDashboardData'
import Header from './appShell/Header/Header'
import MainGrid from './appShell/MainGrid/MainGrid'

function DashboardScreen() {
  // API queries
  // const results = useDashboardData();
  // console.log("results of auto refresh data :",results)
  useAutoRefresh();
  // AUTO REFRESH STARTS HERE
  // useCandleRefresh();

  return (
    <div className="min-h-screen bg-white p-4">
      <Header />
      <MainGrid />
    </div>
  )
}

export default DashboardScreen
