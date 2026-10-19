import { lazy, type ComponentType } from "react";

export interface RetryableLazyHandle<P extends object> {
  current(): ComponentType<P>;
  reset(): void;
}

/**
 * React.lazy permanently remembers a rejected import promise. Keep the lazy
 * component stable until the owner explicitly resets it; an ErrorBoundary can
 * then show the first failure and make its Retry action create a fresh import.
 */
export function retryableLazy<P extends object>(
  load: () => Promise<{ default: ComponentType<P> }>,
): RetryableLazyHandle<P> {
  let Loaded = lazy(load) as unknown as ComponentType<P>;
  return {
    current: () => Loaded,
    reset: () => {
      Loaded = lazy(load) as unknown as ComponentType<P>;
    },
  };
}
