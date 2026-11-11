import { useEffect, useState } from "react";

export const SEARCH_DEBOUNCE_MS = 300;

/** Значение, которое обновляется только после паузы `delay` мс в изменениях. */
export function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}
