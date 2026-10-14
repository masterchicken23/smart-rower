import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from 'react';

import { FunnelStore } from './funnel/store';
import { RowingStore, type Slice } from './store';

const RowingContext = createContext<RowingStore | null>(null);
const FunnelContext = createContext<FunnelStore | null>(null);

export function RowingProvider({ children }: { children: ReactNode }) {
  // The funnel is polled only while a screen holds it (FunnelStore.retain), and the
  // Coxswain store listens only while its screen is open (RowingStore.attach).
  const [funnel] = useState(() => new FunnelStore());
  const [store] = useState(() => new RowingStore(funnel));

  return (
    <RowingContext.Provider value={store}>
      <FunnelContext.Provider value={funnel}>{children}</FunnelContext.Provider>
    </RowingContext.Provider>
  );
}

export function useRowing(): RowingStore {
  const store = useContext(RowingContext);
  if (!store) throw new Error('useRowing must be used inside <RowingProvider>');
  return store;
}

export function useFunnel(): FunnelStore {
  const store = useContext(FunnelContext);
  if (!store) throw new Error('useFunnel must be used inside <RowingProvider>');
  return store;
}

export function useSlice<T>(slice: Slice<T>): T {
  return useSyncExternalStore(slice.subscribe, slice.get, slice.get);
}
