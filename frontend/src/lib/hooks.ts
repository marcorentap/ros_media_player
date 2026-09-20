import { useEffect, useRef, useState } from 'react'

/** Read the current browser path (client-side routing, no dependency). */
export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname + window.location.search)
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname + window.location.search)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  return path
}

/**
 * A mutable ref that always holds the latest value. Lets `[]`-once effects
 * (key handlers, media listeners) read current state without re-subscribing
 * and without a stale closure.
 */
export function useLatest<T>(value: T): { current: T } {
  const ref = useRef(value)
  ref.current = value
  return ref
}