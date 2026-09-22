import React, { type ReactNode } from 'react';
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./queryClient";

interface Props {
  children: ReactNode;
}

export default function Providers({ children }: Props) {
  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );
}