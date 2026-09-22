import { useEffect, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  getS9Override,
  setS9Override,
  resetS9Override,
} from "../../services/s9Api";


export type S9Mode = {
  mode: "auto" | "manual";
  direction: "" | "bullish" | "bearish";
};



export function useS9Mode() {

  const queryClient = useQueryClient();



  /**
   * Fetch current override from backend
   */
  const {
    data: overrideData,
  } = useQuery({

    queryKey:["s9-override"],

    queryFn:getS9Override,

  });



  const [selectedMode,setSelectedMode] =
    useState<S9Mode>({
      mode:"auto",
      direction:""
    });



  /**
   * Sync backend value with dropdown
   */
  useEffect(()=>{

    if(!overrideData) return;


    if(
      overrideData.mode === "manual" &&
      overrideData.manual_direction
    ){

      setSelectedMode({

        mode:"manual",

        direction:
          overrideData.manual_direction

      });


    }
    else{

      setSelectedMode({

        mode:"auto",

        direction:""

      });

    }


  },[overrideData]);




  /**
   * Manual bullish / bearish
   */
  const applyOverride = useMutation({

    mutationFn:setS9Override,


    onSuccess:(response)=>{


      queryClient.invalidateQueries({

        queryKey:["s9-override"]

      });


      queryClient.invalidateQueries({

        queryKey:["s9"]

      });


      if(response?.override){

        setSelectedMode({

          mode:"manual",

          direction:
            response.override.manual_direction

        });

      }


    }

  });





  /**
   * Reset Auto mode
   */
  const resetOverrideMutation = useMutation({

    mutationFn:resetS9Override,


    onSuccess:()=>{


      queryClient.invalidateQueries({

        queryKey:["s9-override"]

      });



      queryClient.invalidateQueries({

        queryKey:["s9"]

      });



      setSelectedMode({

        mode:"auto",

        direction:""

      });


    }

  });






  /**
   * Dropdown change handler
   */
  const changeMode = (
    item:S9Mode
  )=>{


    if(item.mode==="auto"){


      resetOverrideMutation.mutate();


      return;

    }



    applyOverride.mutate(

      item.direction as
      "bullish" | "bearish"

    );


  };






  return {


    selectedMode,


    changeMode,


    loading:

      applyOverride.isPending ||

      resetOverrideMutation.isPending,



    error:

      applyOverride.error ||

      resetOverrideMutation.error


  };

}