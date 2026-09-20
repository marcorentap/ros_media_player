import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Minus, Plus } from 'lucide-react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import { formatTime, clamp01, clampN, roundT, tickLabel } from '../lib/format'
import { LANE_H, RULER_H, ZOOM_MIN_PPS, ZOOM_MAX_PPS } from '../lib/tracks'
import type { TimelineTrack } from '../types'
import { TrackRowHeading } from './TrackRowHeading'

export type TimelineProps = {
  current: number
  duration: number
  buffered: number
  playing: boolean
  tracks: TimelineTrack[] | null
  selectedKey: string | null
  onSeek: (t: number) => void
  onSelectTrack: (key: string) => void
  onRenameTrack: (key: string, name: string) => void
  onTopicTrack: (key: string, topic: string) => void
  onFrameIdTrack: (key: string, frameId: string) => void
  onStampedTrack: (key: string, stamped: boolean) => void
  onAddTrack: () => void
  onRemoveTrack: () => void
}

export function Timeline({
  current,
  duration,
  buffered,
  playing,
  tracks,
  selectedKey,
  onSeek,
  onSelectTrack,
  onRenameTrack,
  onTopicTrack,
  onFrameIdTrack,
  onStampedTrack,
  onAddTrack,
  onRemoveTrack,
}: TimelineProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const sbDragRef = useRef<{ startX: number; startScrollLeft: number } | null>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const [pps, setPps] = useState(32)
  const [scrollLeft, setScrollLeft] = useState(0)
  const [viewportW, setViewportW] = useState(0)
  const [manual, setManual] = useState(false)

  const shown = drag ?? current

  // Measure the scroll viewport so we can auto-fit and cull off-screen grids.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setViewportW(e.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const zoomToFit = useCallback(() => {
    if (duration > 0 && viewportW > 0) {
      setPps(clampN(viewportW / duration, ZOOM_MIN_PPS, ZOOM_MAX_PPS))
    }
  }, [duration, viewportW])

  // Auto-fit to the whole clip on load / when duration changes, unless the
  // user has zoomed by hand.
  useEffect(() => {
    if (!manual) zoomToFit()
  }, [duration, viewportW, manual, zoomToFit])

  const contentW =
    duration > 0
      ? Math.max(1, Math.max(duration * pps, viewportW))
      : Math.max(1, viewportW)

  // Custom scrollbar geometry. The lane area's native scrollbar is hidden, so
  // a track + thumb are drawn below it; the thumb's size reflects the visible
  // fraction and its position mirrors scrollLeft.
  const scrollable = Math.max(0, contentW - viewportW)
  const trackW = viewportW > 0 ? viewportW : 1
  const sbThumbW =
    scrollable > 0 && contentW > 0
      ? Math.max(28, Math.min(trackW, (viewportW / contentW) * trackW))
      : 0
  const sbThumbX =
    scrollable > 0 ? clampN((scrollLeft / scrollable) * (trackW - sbThumbW), 0, trackW - sbThumbW) : 0

  // Major grid lines within the visible span, spacing auto-coarsened with the
  // zoom so lines stay >= 26px apart, and only rendered for the visible window.
  const grid = useMemo(() => {
    if (duration <= 0 || viewportW <= 0 || pps <= 0) return { major: [], step: 1 }
    const majorSteps = [0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
    const step = majorSteps.find((s) => pps * s >= 26) ?? majorSteps[majorSteps.length - 1]
    const startT = Math.max(0, Math.min(duration, scrollLeft / pps))
    const endT = Math.max(0, Math.min(duration, (scrollLeft + viewportW) / pps))
    const majorLines: number[] = []
    for (let t = Math.floor(startT / step) * step; t <= endT + 1e-9; t += step) {
      if (t >= 0) majorLines.push(roundT(t))
    }
    return { major: majorLines, step }
  }, [duration, viewportW, pps, scrollLeft])

  // Keep the playhead in view while playing.
  useEffect(() => {
    if (!playing || duration <= 0) return
    const el = scrollRef.current
    if (!el) return
    const px = shown * pps
    const right = el.clientWidth
    if (px < el.scrollLeft + 6 || px > el.scrollLeft + right - 6) {
      el.scrollLeft = clampN(px - right * 0.45, 0, el.scrollWidth - el.clientWidth)
    }
  }, [playing, shown, pps, viewportW, duration])

  const timeFromEvent = (clientX: number): number => {
    const el = contentRef.current!
    const rect = el.getBoundingClientRect()
    const fx = rect.width > 0 ? (clientX - rect.left) / rect.width : 0
    return clamp01(fx) * duration
  }

  const beginDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    const t = timeFromEvent(e.clientX)
    onSeek(t)
    setDrag(t)
  }
  const duringDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if ((e.buttons & 1) !== 0) {
      const t = timeFromEvent(e.clientX)
      onSeek(t)
      setDrag(t)
    }
  }
  const endDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    onSeek(timeFromEvent(e.clientX))
    setDrag(null)
  }

  // Custom scrollbar dragging. Clicking/tapping the track jumps the thumb so
  // its center lands under the pointer; dragging maps the movement onto the
  // scrollable pixel range of the lane content.
  const beginSb = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    const el = scrollRef.current
    if (!el) return
    const rect = e.currentTarget.getBoundingClientRect()
    const trackX = e.clientX - rect.left
    const denom = Math.max(1, rect.width - sbThumbW)
    const targetFrac = clampN((trackX - sbThumbW / 2) / denom, 0, 1)
    el.scrollLeft = clampN(targetFrac * scrollable, 0, scrollable)
    sbDragRef.current = { startX: e.clientX, startScrollLeft: el.scrollLeft }
  }
  const moveSb = (e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = sbDragRef.current
    const el = scrollRef.current
    if (!drag || !el || (e.buttons & 1) === 0) return
    e.currentTarget.setPointerCapture(e.pointerId)
    const denom = Math.max(1, trackW - sbThumbW)
    const dScroll = ((e.clientX - drag.startX) / denom) * scrollable
    el.scrollLeft = clampN(drag.startScrollLeft + dScroll, 0, scrollable)
  }
  const endSb = () => {
    sbDragRef.current = null
  }

  // Zoom with the mouse wheel, anchored at the cursor: the time under the
  // pointer stays under it while the window zooms in/out. Attached natively
  // with passive:false so default horizontal scrolling is suppressed.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const cursorX = e.clientX - rect.left
      if (duration <= 0 || pps <= 0 || rect.width <= 0) return
      const timeAtCursor = (el.scrollLeft + cursorX) / pps
      const factor = clampN(Math.pow(1.0015, -e.deltaY), 0.5, 2)
      const next = clampN(pps * factor, ZOOM_MIN_PPS, ZOOM_MAX_PPS)
      if (next === pps) return
      setManual(true)
      setPps(next)
      el.scrollLeft = clampN(
        timeAtCursor * next - cursorX,
        0,
        Math.max(0, el.scrollWidth - el.clientWidth),
      )
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [pps, duration])

  return (
    <div className="mt-6 w-full">
      <div className="flex w-full select-none">
        {/* left label column (aligned by shared LANE_H rows) */}
        <div className="w-40 shrink-0 pr-3">
          <div style={{ height: RULER_H }} />
          {/* add / remove track actions, on the same row as the progress lane */}
          <div className="flex items-center gap-1 px-1" style={{ height: LANE_H }}>
            <button
              onClick={onAddTrack}
              aria-label="Add track"
              title="Add track"
              disabled={!tracks}
              className="flex shrink-0 cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-sm text-neutral-400 transition-colors hover:bg-white/5 hover:text-white disabled:cursor-default disabled:opacity-40"
            >
              <Plus size={15} />
            </button>
            <button
              onClick={onRemoveTrack}
              aria-label="Remove selected track"
              title="Remove selected track"
              disabled={!tracks || !selectedKey || (tracks && tracks.length <= 1)}
              className="flex shrink-0 cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-sm text-neutral-400 transition-colors hover:bg-red-500/10 hover:text-red-300 disabled:cursor-default disabled:opacity-40"
            >
              <Minus size={15} />
            </button>
          </div>
          {tracks?.map((t) => (
            <TrackRowHeading
              key={t.key}
              track={t}
              selected={t.key === selectedKey}
              onSelect={() => onSelectTrack(t.key)}
              onRename={(name) => onRenameTrack(t.key, name)}
              onTopic={(topic) => onTopicTrack(t.key, topic)}
              onFrameId={(frameId) => onFrameIdTrack(t.key, frameId)}
              onStamped={(stamped) => onStampedTrack(t.key, stamped)}
            />
          ))}
        </div>

        {/* scrollable lane area (ruler + grids + lanes + playhead) with the
            custom scrollbar track drawn below it */}
        <div className="relative min-w-0 flex-1">
          <div
            ref={scrollRef}
            onScroll={(e) => setScrollLeft(e.currentTarget.scrollLeft)}
            className="scrollbar-none relative overflow-x-auto overflow-y-hidden"
          >
            <div ref={contentRef} className="relative" style={{ width: contentW, minWidth: '100%' }}>
              {/* ruler: labels only on adaptive major gridlines */}
              <div className="relative z-10 overflow-hidden" style={{ height: RULER_H }}>
                {grid.major.map((s) =>
                  s === 0 ? null : (
                    <div
                      key={`l${s}`}
                      className="absolute top-0 -translate-x-1/2 whitespace-nowrap rounded-sm px-1 text-[11px] tabular-nums text-neutral-500"
                      style={{ left: `${s * pps}px`, background: 'var(--page-bg)' }}
                    >
                      {tickLabel(s, grid.step)}
                    </div>
                  ),
                )}
              </div>

              {/* grid layer: major vertical lines across ruler + all lanes */}
              <div className="pointer-events-none absolute inset-0 z-0">
                {grid.major.map((s) => (
                  <div
                    key={`M${s}`}
                    className="absolute inset-y-0 w-px bg-white/[0.13]"
                    style={{ left: `${s * pps}px` }}
                  />
                ))}
              </div>

              {/* progress lane */}
              <div
                className="relative z-10 flex cursor-pointer items-center"
                style={{ height: LANE_H }}
                onPointerDown={beginDrag}
                onPointerMove={duringDrag}
                onPointerUp={endDrag}
                onPointerCancel={() => setDrag(null)}
              >
                <div className="relative h-1.5 w-full">
                  <div className="absolute inset-0 rounded-full bg-white/10" />
                  <div
                    className="absolute inset-y-0 left-0 rounded-full bg-white/15"
                    style={{ width: `${buffered * pps}px` }}
                  />
                  <div
                    className="absolute inset-y-0 left-0 rounded-full bg-blue-500"
                    style={{ width: `${shown * pps}px` }}
                  />
                  <div
                    className="absolute top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full bg-white shadow-md"
                    style={{ left: `${shown * pps - 7}px` }}
                  />
                </div>
              </div>

              {/* track lanes; not clickable for scrubbing, only the markers are */}
              {tracks?.map((t) => (
                <div
                  key={t.key}
                  className={`relative border-b border-white/5 ${
                    t.key === selectedKey ? 'bg-white/[0.07]' : ''
                  }`}
                  style={{ height: LANE_H }}
                >
                  {t.points.map((pt, i) => (
                    <div
                      key={i}
                      title={`${t.name} @ ${formatTime(pt.t)}`}
                      className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full shadow"
                      style={{ left: `${pt.t * pps}px`, background: t.color }}
                      onPointerDown={(e) => e.stopPropagation()}
                      onClick={(e) => {
                        e.stopPropagation()
                        onSeek(pt.t)
                      }}
                    />
                  ))}
                </div>
              ))}

              {/* playhead: thin vertical line spanning the whole timeline */}
              <div
                className="pointer-events-none absolute inset-y-0 z-20 w-px -translate-x-1/2 bg-white/60"
                style={{ left: `${shown * pps}px` }}
              />
            </div>
          </div>

          {/* custom scrollbar (only shown when content overflows the viewport) */}
          {scrollable > 0 && (
            <div
              className="relative mt-1 h-2 w-full cursor-pointer"
              onPointerDown={beginSb}
              onPointerMove={moveSb}
              onPointerUp={endSb}
              onPointerCancel={endSb}
            >
              <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-white/10" />
              <div
                className="absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-white/30 transition-colors hover:bg-white/50"
                style={{ left: sbThumbX, width: sbThumbW }}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}