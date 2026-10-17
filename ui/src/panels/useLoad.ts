// A GET that reloads when its key changes and drops a stale answer, so a fast click through projects or runs never
// shows the previous one's data.

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "./api";

export interface Loaded<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** `load` runs whenever `key` changes (and on `reload()`); a null `key` clears the data and loads nothing. */
export function useLoad<T>(key: string | null, load: () => Promise<T>): Loaded<T> {
  const [state, setState] = useState<{ data: T | null; loading: boolean; error: string | null }>({
    data: null,
    loading: key !== null,
    error: null,
  });
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  // `tick` is the explicit reload trigger
  useEffect(() => {
    if (key === null) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    let live = true;
    setState((s) => ({ data: s.data, loading: true, error: null }));
    loadRef
      .current()
      .then((data) => {
        if (live) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (live) setState({ data: null, loading: false, error: errorMessage(err) });
      });
    return () => {
      live = false;
    };
  }, [key, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
