"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import { createPortal } from "react-dom";

type Mode = {
  mode: "auto" | "manual";
  direction: "" | "bullish" | "bearish";
};

interface S9ModeDropdownProps {
  selectedMode: Mode;

  loading?: boolean;

  onChange: (
    mode: Mode
  ) => void;

  disabled?: boolean;
}


const OPTIONS = [
  {
    label: "Auto",
    mode: "auto",
    direction: "",
  },
  {
    label: "Manual Bullish",
    mode: "manual",
    direction: "bullish",
  },
  {
    label: "Manual Bearish",
    mode: "manual",
    direction: "bearish",
  },
];


export default function S9ModeDropdown({
  selectedMode,
  loading = false,
  onChange,
  disabled = false,
}: S9ModeDropdownProps) {

  const [open, setOpen] = useState(false);

  const ref = useRef<HTMLDivElement | null>(null);

  const [position,setPosition] = useState({
    top:0,
    left:0,
    width:240
  });


  useEffect(()=>{

    if(!open || !ref.current) return;

    const update = ()=>{
      const rect = ref.current!.getBoundingClientRect();

      setPosition({
        top:rect.bottom + 8,
        left:rect.left,
        width:Math.max(rect.width,240)
      });
    };


    update();

    window.addEventListener(
      "scroll",
      update,
      true
    );

    window.addEventListener(
      "resize",
      update
    );


    return ()=>{
      window.removeEventListener(
        "scroll",
        update,
        true
      );

      window.removeEventListener(
        "resize",
        update
      );
    }


  },[open]);



  return (
    <div
      ref={ref}
      className="relative"
    >

      <button
        disabled={disabled}
        onClick={()=>setOpen(v=>!v)}
        className="
        flex items-center gap-2
        rounded-md
        border-gray-200
hover:border-gray-300
        bg-gray-100
        px-3 py-2
        text-xs
        font-semibold
        uppercase
        text-black
        "
      >

        <span>
          {selectedMode.mode}

          {
            selectedMode.direction &&
            ` / ${selectedMode.direction}`
          }

        </span>


        {
          loading &&
          <Loader2
            size={14}
            className="animate-spin"
          />
        }


        <ChevronDown size={14}/>

      </button>



      {
        open &&
        createPortal(

          <div
          className="
          fixed
          z-[99999]
          rounded-md
          border
          border-gray-200
          hover:border-gray-300

          bg-gray-100
          shadow-2xl
          "
          style={{
            top:position.top,
            left:position.left,
            width:position.width
          }}
          >

            {
              OPTIONS.map(item=>{

                const active =
                  selectedMode.mode===item.mode &&
                  selectedMode.direction===item.direction;


                return (

                  <button

                  key={item.label}

                  disabled={disabled}

                  onClick={()=>{

                    onChange({
                      mode:item.mode as any,
                      direction:item.direction as any
                    });

                    setOpen(false);

                  }}

                  className={`
  flex
  w-full
  items-center
  justify-between
  px-4
  py-3
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

                    <span>
                      {item.label}
                    </span>


                    {
                      active &&
                      <Check
                      size={16}
                      className="text-blue"
                      />
                    }


                  </button>

                )

              })
            }


          </div>,

          document.body
        )
      }

    </div>
  )
}