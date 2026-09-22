import { useMinuteStore } from "../stores/minuteStore";


function MinuteSelector() {
  const { minute, setMinute } = useMinuteStore();

  return (
    <div className="flex gap-2">

      <button
        onClick={() => setMinute("5m")}
        className={minute === "5m" ? "bg-blue-500" : ""}
      >
        5m
      </button>

      <button
        onClick={() => setMinute("10m")}
        className={minute === "10m" ? "bg-blue-500" : ""}
      >
        10m
      </button>

      <button
        onClick={() => setMinute("15m")}
        className={minute === "15m" ? "bg-blue-500" : ""}
      >
        15m
      </button>

    </div>
  );
}