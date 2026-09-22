import { useHealth } from "../../../../features/hooks/useHealth";
// import { statusWidgetData } from '../headerContent'

function StatusWidget() {
  const { data, isLoading } = useHealth();
  // console.log(data);

  const statusWidgetData = {
    label: "Market Status",
    value: data?.market.is_open,
    detail: {
      open: data?.market.hours.open,
      close: data?.market.hours.close,
    },
  };

  return (
    <div className="flex h-full flex-col justify-center items-start
        lg:items-center">
      <p className="text-[0.60rem] uppercase tracking-[0.24em] text-text-muted">
        {statusWidgetData.label}
      </p>

      <div className=" flex items-center gap-2">
        <span
          className={`h-2 w-2 rounded-full ${statusWidgetData.value ? "bg-green" : "bg-red"}`}
        />
        <span
          className={`text-[0.95rem] font-semibold ${statusWidgetData.value ? "text-green" : "text-red"}`}
        >
          {statusWidgetData.value ? "OPEN " : "CLOSE"}
          {/* green */}
        </span>
      </div>

      <p className="mt-1 text-xs text-text-secondary">
        Open {statusWidgetData.detail.open} - {statusWidgetData.detail.close}
      </p>
    </div>
  );
}

export default StatusWidget;
