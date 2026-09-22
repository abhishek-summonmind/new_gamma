import { marketHeroData } from "../headerContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { refresh } from "../../../../services/refreshApi";
import {
  getSelectedIndexSymbol,
  setSelectedIndexSymbol,
  SUPPORTED_INDEX_SYMBOLS,
} from "../../../../services/indexSelection";
import { useEffect, useState } from "react";
import { ChevronDown, Check, RefreshCw } from "lucide-react";

function MarketStats() {
  const queryClient = useQueryClient();

  const [selectedIndex, setSelectedIndex] = useState(
    getSelectedIndexSymbol()
  );

  const [open, setOpen] = useState(false);

  const refreshMutation = useMutation({
    mutationFn: refresh,
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["s1"] });
      queryClient.invalidateQueries({ queryKey: ["s9"] });
    },
  });

  useEffect(() => {
    const onChange = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail) setSelectedIndex(detail);
    };

    window.addEventListener("gamma:index-symbol-change", onChange);

    return () =>
      window.removeEventListener("gamma:index-symbol-change", onChange);
  }, []);

  const onIndexChange = (value: string) => {
    const next = setSelectedIndexSymbol(value);
    setSelectedIndex(next);
    setOpen(false);
    refreshMutation.mutate(next);
  };

  return (
    <div className="flex h-full flex-col">
      <p className="text-xs font-semibold uppercase text-black">
        {marketHeroData.marketLabel}
      </p>

      <div className="mt-2 flex items-center gap-2">
        {/* Dropdown */}
        <div className="relative">
          <button
            onClick={() => setOpen((v) => !v)}
            className="
              flex
              h-9
              min-w-[150px]
              items-center
              justify-between
              rounded-[8px]
              border
              border-gray-200
              bg-gray-100
              px-3
              text-sm
              font-semibold
              text-gray-900
              transition-all
              hover:border-gray-300
            "
          >
            <span>{selectedIndex}</span>

            <ChevronDown
              size={16}
              className={`transition-transform duration-200 ${
                open ? "rotate-180" : ""
              }`}
            />
          </button>

          {open && (
            <div
              className="
                absolute
                left-0
                top-full
                z-50
                mt-2
                w-full
                overflow-hidden
                rounded-[8px]
                border
                border-gray-200
                bg-gray-100
                shadow-lg
              "
            >
              {SUPPORTED_INDEX_SYMBOLS.map((symbol) => {
                const active = symbol === selectedIndex;

                return (
                  <button
                    key={symbol}
                    onClick={() => onIndexChange(symbol)}
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
                        active
                          ? "bg-blue-500/10 text-blue"
                          : "text-gray-900 hover:bg-gray-200"
                      }
                    `}
                  >
                    {symbol}

                    {active && (
                      <Check
                        size={15}
                        className="text-blue"
                      />
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Refresh Button */}
        {refreshMutation.isPending && (
        <button
          onClick={() => refreshMutation.mutate(selectedIndex)}
          disabled={refreshMutation.isPending}
          className="
            flex
            h-9
            w-9
            items-center
            justify-center
            rounded-[8px]
            border
            border-gray-200
            bg-gray-100
            transition-all
            hover:border-gray-300
            hover:bg-gray-200
            disabled:cursor-not-allowed
            disabled:opacity-70
          "
        >
          <RefreshCw
            size={16}
            className={
              refreshMutation.isPending
                ? "animate-spin text-blue-600"
                : "text-gray-700"
            }
          />
        </button>


        )}
      </div>
    </div>
  );
}

export default MarketStats;