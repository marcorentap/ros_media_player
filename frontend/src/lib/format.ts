// Pure formatting/geometry helpers for the video player UI.

export function formatTime(t: number): string {
  if (!isFinite(t) || t < 0) t = 0
  const ms = Math.floor((t % 1) * 1000)
  const totalS = Math.floor(t)
  const h = Math.floor(totalS / 3600)
  const m = Math.floor((totalS % 3600) / 60)
  const s = totalS % 60
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s
    .toString()
    .padStart(2, '0')}.${ms.toString().padStart(3, '0')}`
}

/** Title shortening: keep the extension visible, ellipsize the middle. */
export function truncateName(name: string, max = 48): string {
  if (name.length <= max) return name
  const ext = name.match(/\.\w+$/)?.[0] ?? ''
  const stem = name.slice(0, name.length - ext.length)
  const room = max - ext.length - 1 // -1 for the ellipsis
  if (room <= 2) return name.slice(0, max - 1) + '…'
  return stem.slice(0, room - 1) + '…' + ext
}

export function clamp01(n: number): number {
  return Math.min(1, Math.max(0, n))
}

export function clampN(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n))
}

// Round to 1 ms so float hair-lines don't collide with duplicate keys.
export function roundT(t: number): number {
  return Math.round(t * 1000) / 1000
}

// Label for a ruler tick: for whole-second steps show a bare integer, but for
// sub-second steps keep as many decimals as the step implies (0.05 -> 2 decimal
// places, 0.01 -> 2, 0.002 -> 3) so adjacent ticks never repeat the same text.
export function tickLabel(t: number, step: number): string {
  if (step >= 1) return String(Math.round(t))
  let decimals = 0
  let s = step
  while (s < 1) {
    s *= 10
    decimals++
  }
  if (Math.round(t) === t) return String(t)
  return t.toFixed(decimals).replace(/0+$/, '')
}