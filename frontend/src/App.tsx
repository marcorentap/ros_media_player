import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { createPortal } from 'react-dom'
import {
  ArrowLeft,
  Minus,
  Pause,
  Pencil,
  Play,
  Plus,
  Repeat,
  Settings2,
  Square,
  Trash2,
  Upload,
  X,
} from 'lucide-react'

type MediaItem = {
  id: string
  name: string
  mime: string
  size: number
  kind: 'image' | 'video'
}

const IMAGE_RE = /\.(jpe?g|png|gif|webp|bmp|svg)$/i
const VIDEO_RE = /\.(mp4|webm|mov|mkv|avi|ogg)$/i

function isAllowed(name: string): boolean {
  return IMAGE_RE.test(name) || VIDEO_RE.test(name)
}

function mediaUrl(id: string): string {
  return `/media/${id}`
}

function playerUrl(id: string): string {
  return `/player/${id}`
}

/** Title shortening: keep the extension visible, ellipsize the middle. */
function truncateName(name: string, max = 48): string {
  if (name.length <= max) return name
  const ext = name.match(/\.\w+$/)?.[0] ?? ''
  const stem = name.slice(0, name.length - ext.length)
  const room = max - ext.length - 1 // -1 for the ellipsis
  if (room <= 2) return name.slice(0, max - 1) + '…'
  return stem.slice(0, room - 1) + '…' + ext
}

let dragDepth = 0

// --- tiny path router ------------------------------------------------------

function usePath(): string {
  const [path, setPath] = useState(window.location.pathname + window.location.search)
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname + window.location.search)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  return path
}

const PLAYER_PREFIX = '/player/'

// --- gallery (home) --------------------------------------------------------

function Gallery() {
  const [items, setItems] = useState<MediaItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/media')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const body = await res.json()
      setItems(Array.isArray(body.media) ? body.media : [])
      setError(null)
    } catch (e) {
      setError(`Could not load media: ${(e as Error).message}`)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const upload = useCallback(
    async (files: File[] | FileList) => {
      const list = Array.from(files)
      if (!list.length) return
      const rejected = list.filter((f) => !isAllowed(f.name))
      if (rejected.length) {
        setError(`Only pictures and videos. Skipped: ${rejected.map((f) => f.name).join(', ')}`)
      }
      const allowed = list.filter((f) => isAllowed(f.name))
      if (!allowed.length) return

      setUploading(true)
      setError(null)
      for (let i = 0; i < allowed.length; i++) {
        try {
          const fd = new FormData()
          fd.append('files', allowed[i])
          const res = await fetch('/api/media', { method: 'POST', body: fd })
          if (!res.ok) {
            const body = (await res.json().catch(() => null)) as { error?: string } | null
            throw new Error(body?.error ?? `HTTP ${res.status}`)
          }
        } catch (e) {
          setError(`Upload "${allowed[i].name}" failed: ${(e as Error).message}`)
          break
        }
      }
      setUploading(false)
      await load()
    },
    [load],
  )

  // Make the whole page a drop target with a full-page overlay while dragging.
  useEffect(() => {
    const onEnter = (e: DragEvent) => {
      e.preventDefault()
      dragDepth += 1
      setDragging(true)
    }
    const onOver = (e: DragEvent) => {
      e.preventDefault()
    }
    const onLeave = (e: DragEvent) => {
      e.preventDefault()
      dragDepth = Math.max(0, dragDepth - 1)
      if (dragDepth === 0) setDragging(false)
    }
    const onDrop = (e: DragEvent) => {
      e.preventDefault()
      dragDepth = 0
      setDragging(false)
      if (e.dataTransfer) upload(e.dataTransfer.files)
    }

    window.addEventListener('dragenter', onEnter)
    window.addEventListener('dragover', onOver)
    window.addEventListener('dragleave', onLeave)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onEnter)
      window.removeEventListener('dragover', onOver)
      window.removeEventListener('dragleave', onLeave)
      window.removeEventListener('drop', onDrop)
    }
  }, [upload])

  return (
    <main className="flex min-h-screen w-full flex-col px-7 py-6">
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex flex-col items-center justify-center gap-2 bg-blue-600/40 backdrop-blur-sm">
          <p className="text-xl font-semibold text-white drop-shadow">Drop to upload</p>
          <p className="text-sm text-white/80">Pictures and videos</p>
        </div>
      )}

      <header className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Media</h1>
        <button
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
          className="flex h-7 cursor-pointer items-center gap-1.5 rounded-full px-3 text-sm text-white transition-colors hover:bg-(--btn-bg-hover) disabled:bg-(--btn-bg-disabled) disabled:text-(--btn-text-disabled)"
          style={{ backgroundColor: 'var(--btn-bg)' }}
        >
          <Upload size={14} />
          {uploading ? 'Uploading…' : 'Upload'}
        </button>
        <input
          ref={inputRef}
          type="file"
          accept="image/*,video/*"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) upload(e.target.files)
            e.target.value = ''
          }}
        />
      </header>

      <div className="grid grid-cols-2 content-start items-start gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
        {items.length === 0 && !uploading && (
          <p className="col-span-full py-16 text-center text-sm text-neutral-500">
            Drop pictures or videos here to upload.
          </p>
        )}

        {items.map((item) => (
          <figure key={item.id} className="relative m-0">
            <a
              href={playerUrl(item.id)}
              className="block"
              title={`Open ${item.name}`}
            >
              <div className="aspect-video cursor-pointer overflow-hidden rounded-md bg-(--thumb-bg)">
                {item.kind === 'video' ? (
                  <video
                    src={mediaUrl(item.id)}
                    muted
                    preload="metadata"
                    draggable={false}
                    className="h-full w-full object-contain"
                  />
                ) : (
                  <img
                    src={mediaUrl(item.id)}
                    alt={item.name}
                    loading="lazy"
                    draggable={false}
                    className="h-full w-full object-contain"
                  />
                )}
              </div>
            </a>
            <figcaption
              className="truncate px-2 py-1.5 text-center text-xs text-neutral-300"
              title={item.name}
            >
              {item.name}
            </figcaption>
          </figure>
        ))}

        {uploading && <p className="col-span-full py-4 text-center text-sm text-blue-400">Uploading…</p>}
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    </main>
  )
}

// --- scrubber --------------------------------------------------------------

function formatTime(t: number): string {
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

// A marker pinned to a moment of playback. t is seconds; x,y are the click
// position normalized to the video element box (0..1) so the same dot can be
// drawn back on top of the video when the playhead passes t.
type TimelinePoint = {
  t: number
  x: number
  y: number
}

type TimelineTrack = {
  key: string
  name: string
  color: string
  topic: string
  frameId: string
  stamped: boolean
  points: TimelinePoint[]
}

// Kelly's "Twenty-Two Colors of Maximum Contrast" (Kelly, 1965), as published
// and implemented by the R `Polychrome` package. The full 22-color set includes
// Black (#222222) and White (#f2f3f4); both are dropped here (per the request),
// leaving the 20 chromatic colors in Kelly's original optimal-contrast ordering.
// With 20 entries the cycles rarely wrap, but when they do each full cycle
// (`cycle` >= 1) darkens the color toward black to keep repeats identifiable
// against the dark timeline background.
const TRACK_COLORS = [
  '#f3c300', // Vivid Yellow
  '#875692', // Strong Purple
  '#f38400', // Vivid Orange
  '#a1caf1', // Very Light Blue
  '#be0032', // Vivid Red
  '#c2b280', // Grayish Yellow
  '#848482', // Medium Gray
  '#008856', // Vivid Green
  '#e68fac', // Strong Purplish Pink
  '#0067a5', // Strong Blue
  '#f99379', // Strong Yellowish Pink
  '#604e97', // Strong Violet
  '#f6a600', // Vivid Orange Yellow
  '#b3446c', // Strong Purplish Red
  '#dcd300', // Vivid Greenish Yellow
  '#882d17', // Strong Reddish Brown
  '#8db600', // Vivid Yellowish Green
  '#654522', // Deep Yellowish Brown
  '#e25822', // Vivid Reddish Orange
  '#2b3d26', // Strong Olive Green
]
const TRACK_COLOR_DARKEN_PER_CYCLE = 0.28 // darkening multiplier per full wrap (capped)

// Hardcoded track set for now. Names/colors persist once renamed on a given
// media item; a fresh media item falls back to these defaults.
const DEFAULT_TRACKS: TimelineTrack[] = [
  { key: 'a', name: 'Object 1', color: TRACK_COLORS[0], topic: '/media_player/click', frameId: 'media_player', stamped: true, points: [] },
]

function darkenHex(hex: string, amount: number): string {
  const n = parseInt(hex.slice(1), 16)
  const r = Math.round(((n >> 16) & 255) * (1 - amount))
  const g = Math.round(((n >> 8) & 255) * (1 - amount))
  const b = Math.round((n & 255) * (1 - amount))
  return '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')
}

// Color for the track at the given ordinal: base Kelly palette entry, darkened
// on each palette wrap so repeats stay distinguishable.
function trackColor(index: number): string {
  const base = TRACK_COLORS[index % TRACK_COLORS.length]
  const cycle = Math.floor(index / TRACK_COLORS.length)
  return cycle === 0
    ? base
    : darkenHex(base, Math.min(cycle, 3) * TRACK_COLOR_DARKEN_PER_CYCLE)
}

const LANE_H = 32 // px height shared by every lane row for column alignment
const RULER_H = 18 // px height of the time ruler above the lanes

// Zoom (pixels per second) limits for the scrollable timeline. The upper bound
// is high enough that the finest sub-second grid step (10 ms) can be selected
// once the lines are >= 26px apart.
const ZOOM_MIN_PPS = 5
const ZOOM_MAX_PPS = 2600

function clamp01(n: number): number {
  return Math.min(1, Math.max(0, n))
}

function clampN(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n))
}

// Round to 1 ms so float hair-lines don't collide with duplicate keys.
function roundT(t: number): number {
  return Math.round(t * 1000) / 1000
}

// Label for a ruler tick: for whole-second steps show a bare integer, but for
// sub-second steps keep as many decimals as the step implies (0.05 -> 2 decimal
// places, 0.01 -> 2, 0.002 -> 3) so adjacent ticks never repeat the same text.
function tickLabel(t: number, step: number): string {
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

// One track row: color chip, name, settings gear, and a rename popup.
function TrackRowHeading({
  track,
  selected,
  onSelect,
  onRename,
  onTopic,
  onFrameId,
  onStamped,
}: {
  track: TimelineTrack
  selected: boolean
  onSelect: () => void
  onRename: (name: string) => void
  onTopic: (topic: string) => void
  onFrameId: (frameId: string) => void
  onStamped: (stamped: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(track.name)
  const [topicDraft, setTopicDraft] = useState(track.topic)
  const [frameIdDraft, setFrameIdDraft] = useState(track.frameId)
  const [stampedDraft, setStampedDraft] = useState(track.stamped)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    setDraft(track.name)
    setTopicDraft(track.topic)
    setFrameIdDraft(track.frameId)
    setStampedDraft(track.stamped)
    const t = setTimeout(() => inputRef.current?.focus(), 0)
    return () => clearTimeout(t)
  }, [open, track.name, track.topic, track.frameId, track.stamped])

  // Close on Escape while the dialog is open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const commit = (e?: { preventDefault(): void }) => {
    e?.preventDefault()
    const next = draft.trim()
    if (next && next !== track.name) onRename(next)
    const t = topicDraft.trim()
    if (t !== track.topic) onTopic(t)
    const f = frameIdDraft.trim()
    if (f !== track.frameId) onFrameId(f)
    if (stampedDraft !== track.stamped) onStamped(stampedDraft)
    setOpen(false)
  }

  return (
    <div
      onClick={onSelect}
      className={`relative flex cursor-pointer items-center gap-1.5 border-b border-white/5 px-2 ${
        selected ? 'bg-white/5' : ''
      }`}
      style={{ height: LANE_H }}
    >
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ background: track.color }}
      />
      <span
        className={`min-w-0 flex-1 truncate text-xs ${
          selected ? 'text-white' : 'text-neutral-400'
        }`}
        title={track.name}
      >
        {track.name}
      </span>
      <button
        onClick={(e) => {
          e.stopPropagation()
          setOpen(true)
        }}
        aria-label={`Settings for ${track.name}`}
        title="Track settings"
        className={`shrink-0 cursor-pointer rounded p-0.5 transition-colors hover:bg-white/10 hover:text-white ${
          open ? 'text-white' : 'text-neutral-500'
        }`}
      >
        <Settings2 size={12} />
      </button>

      {open &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setOpen(false)
            }}
            role="dialog"
            aria-modal="true"
            aria-label={`Track settings: ${track.name}`}
          >
            <div className="w-full max-w-sm rounded-xl border border-white/10 bg-[#0e1116] p-5 shadow-2xl">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Track Settings</h2>
                <button
                  onClick={() => setOpen(false)}
                  aria-label="Close"
                  className="cursor-pointer rounded-md p-1 text-neutral-500 transition-colors hover:bg-white/10 hover:text-white"
                >
                  <X size={16} />
                </button>
              </div>
              <label className="block text-[13px] font-medium text-neutral-300">
                Name
              </label>
              <input
                ref={inputRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commit()
                  else if (e.key === 'Escape') setOpen(false)
                }}
                className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
              />
              <label className="mt-4 block text-[13px] font-medium text-neutral-300">
                ROS topic
              </label>
              <input
                value={topicDraft}
                onChange={(e) => setTopicDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commit()
                  else if (e.key === 'Escape') setOpen(false)
                }}
                placeholder="/media_player/click"
                className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
              />
              <label className="mt-4 block text-[13px] font-medium text-neutral-300">
                Frame ID
              </label>
              <input
                value={frameIdDraft}
                onChange={(e) => setFrameIdDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commit()
                  else if (e.key === 'Escape') setOpen(false)
                }}
                placeholder="media_player"
                className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
              />
              <div className="mt-4 flex items-center justify-between">
                <label className="text-[13px] font-medium text-neutral-300">Stamped</label>
                <button
                  role="switch"
                  aria-checked={stampedDraft}
                  aria-label="Stamped"
                  onClick={() => setStampedDraft((s) => !s)}
                  className={`relative h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors ${stampedDraft ? 'bg-blue-600' : 'bg-white/15'}`}
                >
                  <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${stampedDraft ? 'left-[18px]' : 'left-0.5'}`} />
                </button>
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setOpen(false)}
                  className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-white/10"
                >
                  Cancel
                </button>
                <button
                  onClick={commit}
                  className="cursor-pointer rounded-md bg-blue-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-blue-500"
                >
                  Save
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}

type TimelineProps = {
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

function Timeline({
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
  // Scroll + zoom state. Zoom is pixels-per-second; the lane area scrolls
  // horizontally whenever the content is wider than the viewport.
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
  // with passive:false so default horizontal scrolling is suppressed while
  // zooming (pure vertical wheel scroll is left to the scrollbar/touch).
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const cursorX = e.clientX - rect.left
      // Guard against degenerate 0-time or 0-width cursor / viewport pos.
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
          <div
            className="flex items-center gap-1 px-1"
            style={{ height: LANE_H }}
          >
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

              {/* grid layer: major vertical lines across ruler + all lanes, behind them */}
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

// --- video player with timeline --------------------------------------------

function VideoPlayer({ id, deselectSignal }: { id: string; deselectSignal: number }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [buffered, setBuffered] = useState(0)
  const [loop, setLoop] = useState(false)
  const [tracks, setTracks] = useState<TimelineTrack[] | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [playerTopic, setPlayerTopic] = useState('/media_player/image')
  const [playerFrameId, setPlayerFrameId] = useState('media_player')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [topicDraft, setTopicDraft] = useState('')
  const [frameIdDraft, setFrameIdDraft] = useState('')

  // Frame duration, measured live via requestVideoFrameCallback so stepping
  // advances exactly one video frame (falls back to ~30fps before playback).
  const frameDurRef = useRef(1 / 30)
  const lastStepRef = useRef(0)

  // Mirror refs so envelope saves (which fire from callbacks with stale
  // closures) always persist the complete state, never clobber one field.
  const tracksRef = useRef<TimelineTrack[] | null>(tracks)
  tracksRef.current = tracks
  const topicRef = useRef(playerTopic)
  topicRef.current = playerTopic
  const frameIdRef = useRef(playerFrameId)
  frameIdRef.current = playerFrameId

  // Persist the full timeline envelope { tracks, topic, frame_id } after every
  // change. Each mutation passes the slice it just built plus the current
  // other fields (read from the mirror refs, since the mutation only touched
  // one slice). The image is a sensor_msgs/Image, so it's always stamped; only
  // the per-track stamped flags are toggleable.
  const saveTimeline = useCallback(
    (next: TimelineTrack[], topic: string, frameId: string) => {
      fetch(`/api/timeline/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tracks: next, topic, frame_id: frameId }),
      }).catch(() => {})
    },
    [id],
  )

  // Load persisted timeline state for this media item.
  useEffect(() => {
    let alive = true
    setTracks(null)
    fetch(`/api/timeline/${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => {
        if (!alive) return
        // Default tracks only apply to new media (nothing persisted yet). Once
        // a media item has any saved timeline, use exactly what it persisted.
        const stored = Array.isArray(body?.tracks) && body.tracks.length
          ? body.tracks
          : DEFAULT_TRACKS
        setTracks(stored)
        setSelectedKey((prev) => prev ?? stored[0]?.key ?? null)
        if (typeof (body as { topic?: unknown })?.topic === 'string') {
          setPlayerTopic((body as { topic?: string }).topic ?? '')
        }
        if (typeof (body as { frame_id?: unknown })?.frame_id === 'string') {
          setPlayerFrameId((body as { frame_id?: string }).frame_id ?? 'media_player')
        }
      })
      .catch(() => {
        if (!alive) return
        setTracks(DEFAULT_TRACKS)
        setSelectedKey((prev) => prev ?? DEFAULT_TRACKS[0]?.key ?? null)
      })
    return () => {
      alive = false
    }
  }, [id])

  // Persist the full timeline state after every change.
  // (saveTimeline is now the envelope saver defined above.)

  // Set the player-level ROS topic + frame id and persist immediately.
  const changePlayerTopic = useCallback(
    (topic: string, frameId: string) => {
      setPlayerTopic(topic)
      setPlayerFrameId(frameId)
      saveTimeline(tracksRef.current ?? [], topic, frameId)
    },
    [saveTimeline],
  )

  // Set a single track's ROS topic and persist.
  const changeTrackTopic = useCallback(
    (key: string, topic: string) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, topic } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  // Set a single track's frame id and persist.
  const changeTrackFrameId = useCallback(
    (key: string, frameId: string) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, frameId } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  // Set a single track's stamped flag and persist.
  const changeTrackStamped = useCallback(
    (key: string, stamped: boolean) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, stamped } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  // Sync the settings dialog drafts whenever it opens / values change.
  useEffect(() => {
    if (settingsOpen) {
      setTopicDraft(playerTopic)
      setFrameIdDraft(playerFrameId)
    }
  }, [settingsOpen, playerTopic, playerFrameId])

  const commitPlayerTopic = useCallback(() => {
    changePlayerTopic(topicDraft.trim(), frameIdDraft.trim())
    setSettingsOpen(false)
  }, [changePlayerTopic, topicDraft, frameIdDraft])

  // Send a playback control to the backend. The backend owns the decode + ROS
  // publish cursor; the browser only says "play/pause/stop/scrub at time t".
  const sendControl = useCallback((cmd: 'play' | 'pause' | 'stop' | 'scrub', t: number) => {
    const v = videoRef.current
    const w = v?.videoWidth ?? 0
    const h = v?.videoHeight ?? 0
    const fps = frameDurRef.current ? Math.round(1 / frameDurRef.current) : 30
    fetch('/publish/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        media_id: id,
        cmd,
        t,
        width: w,
        height: h,
        fps,
        topic: topicRef.current,
        frame_id: frameIdRef.current,
      }),
    }).catch(() => {})
  }, [id])

  // Mirror so `[]`-once effects (key handling, media events) can fire controls
  // without capturing a stale sendControl closure.
  const sendControlRef = useRef(sendControl)
  sendControlRef.current = sendControl

  const addMarker = useCallback(
    (key: string, point: TimelinePoint) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) =>
          t.key === key ? { ...t, points: [...t.points, point] } : t,
        )
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  const removeMarker = useCallback(
    (key: string, point: TimelinePoint) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) =>
          t.key === key
            ? { ...t, points: t.points.filter((p) => p !== point) }
            : t,
        )
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  const selectTrack = useCallback((key: string) => setSelectedKey(key), [])

  // Unselect the current track when the user clicks away (empty space outside
  // the timeline); MediaPage bumps `deselectSignal` on a background click.
  useEffect(() => {
    if (deselectSignal > 0) setSelectedKey(null)
  }, [deselectSignal])

  const renameTrack = useCallback(
    (key: string, name: string) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, name } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  const addTrack = useCallback(() => {
    if (!tracks) return
    const key = crypto.randomUUID()
    // Insert the new track directly after the selected one (top of the list
    // when nothing is selected), rather than appending to the end.
    const selectedIndex = selectedKey
      ? tracks.findIndex((t) => t.key === selectedKey)
      : -1
    const insertAt = selectedIndex >= 0 ? selectedIndex + 1 : 0
    const created: TimelineTrack = {
      key,
      name: `Track ${tracks.length + 1}`,
      color: trackColor(tracks.length),
      topic: '/media_player/click',
      frameId: 'media_player',
      stamped: true,
      points: [],
    }
    const next: TimelineTrack[] = [
      ...tracks.slice(0, insertAt),
      created,
      ...tracks.slice(insertAt),
    ]
    setTracks(next)
    setSelectedKey(key)
    saveTimeline(next, topicRef.current, frameIdRef.current)
  }, [tracks, selectedKey, saveTimeline])

  const removeTrack = useCallback(() => {
    if (!tracks || !selectedKey || tracks.length <= 1) return
    // The track above the one being removed (original array order), falling
    // back to the topmost remaining track when the removed one was first.
    const removedIndex = tracks.findIndex((t) => t.key === selectedKey)
    const next = tracks.filter((t) => t.key !== selectedKey)
    const above = removedIndex > 0 ? next[removedIndex - 1] : null
    setTracks(next)
    setSelectedKey(above?.key ?? next[0]?.key ?? null)
    saveTimeline(next, topicRef.current, frameIdRef.current)
  }, [tracks, selectedKey, saveTimeline])

  // Clicking the video drops a marker on the selected track at playhead time,
  // remembering where on the frame the click landed.
  const onVideoClick = useCallback(
    (e: ReactMouseEvent<HTMLVideoElement>) => {
      if (!tracks || !selectedKey) return
      const rect = e.currentTarget.getBoundingClientRect()
      const x = rect.width > 0 ? clamp01((e.clientX - rect.left) / rect.width) : 0
      const y = rect.height > 0 ? clamp01((e.clientY - rect.top) / rect.height) : 0
      addMarker(selectedKey, { t: current, x, y })
    },
    [tracks, selectedKey, current, addMarker],
  )

  // Keep the video element's loop flag in sync with state, and pause on
  // ended when looping is off (native ended control handles loop itself).
  useEffect(() => {
    const v = videoRef.current
    if (v) v.loop = loop
  }, [loop])

  // Wire up media events once.
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    const onTime = () => setCurrent(v.currentTime)
    const onDur = () => {
      if (isFinite(v.duration)) setDuration(v.duration)
    }
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onProgress = () => {
      const b = v.buffered
      if (b.length) setBuffered(b.end(b.length - 1))
    }
    const onEnded = () => {
      setPlaying(false)
      sendControlRef.current('stop', v.currentTime)
    }

    v.addEventListener('timeupdate', onTime)
    v.addEventListener('durationchange', onDur)
    v.addEventListener('play', onPlay)
    v.addEventListener('pause', onPause)
    v.addEventListener('progress', onProgress)
    v.addEventListener('ended', onEnded)
    return () => {
      v.removeEventListener('timeupdate', onTime)
      v.removeEventListener('durationchange', onDur)
      v.removeEventListener('play', onPlay)
      v.removeEventListener('pause', onPause)
      v.removeEventListener('progress', onProgress)
      v.removeEventListener('ended', onEnded)
    }
  }, [])

  const seek = useCallback((t: number) => {
    const v = videoRef.current
    if (!v || !isFinite(t)) return
    v.currentTime = t
    setCurrent(t)
    sendControl('scrub', t)
  }, [sendControl])

  const toggle = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) {
      v.play().catch(() => {})
      sendControl('play', v.currentTime)
    } else {
      v.pause()
      sendControl('pause', v.currentTime)
    }
  }, [sendControl])

  const stop = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    v.pause()
    v.currentTime = 0
    setCurrent(0)
    sendControl('stop', 0)
  }, [sendControl])

  // Frame duration, measured live via requestVideoFrameCallback so stepping
  // advances exactly one video frame (falls back to ~30fps before playback).
  useEffect(() => {
    const v = videoRef.current
    if (!v || typeof (v as HTMLVideoElement).requestVideoFrameCallback !== 'function') return
    let last = -1
    const onRvf = (_now: number, meta: { mediaTime: number }) => {
      if (last >= 0 && meta.mediaTime > last) frameDurRef.current = meta.mediaTime - last
      last = meta.mediaTime
      ;(v as HTMLVideoElement).requestVideoFrameCallback(onRvf)
    }
    ;(v as HTMLVideoElement).requestVideoFrameCallback(onRvf)
    return () => {
      last = -1
    }
  }, [])

  // Keep the time display live. The native `timeupdate` event only fires a few
  // times per second, which is too coarse to render milliseconds, so tick the
  // current time from the video element on each animation frame while playing.
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    let raf = 0
    const tick = () => {
      if (!v.paused) setCurrent(v.currentTime)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])

  // Arrow keys step by one frame. Held repeats are throttled to ~15fps (one
  // step every 67ms; key auto-repeat fires ~every 30ms, faster than the
  // decoder can render a seek). Queueing up multiple coalesced seeks is what
  // inflated the live frameDur measurement and made later steps jump seconds.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      // Don't hijack keys while the user is typing in an input/text field
      // (the rename dialog is portaled to body, so it bubbles up here).
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      const v = videoRef.current
      if (!v) return
      const key = (e as KeyboardEvent).key
      if (key === ' ') {
        e.preventDefault()
        if (v.paused) {
          v.play().catch(() => {})
          sendControlRef.current('play', v.currentTime)
        } else {
          v.pause()
          sendControlRef.current('pause', v.currentTime)
        }
      } else if (key === 'ArrowRight' || key === 'ArrowLeft') {
        e.preventDefault()
        const dir = key === 'ArrowRight' ? 1 : -1
        const now = performance.now()
        // Throttle held repeating to ~15fps (one step every 67ms) instead of
        // queueing every auto-repeat seek faster than the decoder can render.
        if (now - lastStepRef.current < 1000 / 15) return
        lastStepRef.current = now
        if (!v.paused) v.pause()
        const t = v.currentTime + dir * frameDurRef.current
        const nextT = dir === 1 ? Math.min(v.duration || 0, t) : Math.max(0, t)
        v.currentTime = nextT
        sendControlRef.current('scrub', nextT)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      <div className="relative aspect-video mx-auto max-h-[420px] w-full max-w-[747px] overflow-hidden rounded-lg bg-black">
        <video
          ref={videoRef}
          src={mediaUrl(id)}
          onClick={onVideoClick}
          className="h-full w-full cursor-crosshair object-contain"
          controls={false}
          playsInline
        />
        {tracks && selectedKey && tracks.length > 0 && (
          <div className="pointer-events-none absolute left-2 top-2 rounded bg-black/50 px-2 py-0.5 text-[10px] text-neutral-300">
            Click to add or remove markers on{' '}
            <span
              className="font-medium"
              style={{ color: tracks.find((t) => t.key === selectedKey)?.color }}
            >
              {tracks.find((t) => t.key === selectedKey)?.name}
            </span>
          </div>
        )}
        {/* overlay dots flashing on the video as the playhead passes each marker; clicking one removes it */}
        {(tracks ?? [])
          .flatMap((t) =>
            t.points
              .filter((p) => Math.abs(current - p.t) <= 0.05)
              .map((p) => ({ point: p, trackKey: t.key, color: t.color })),
          )
          .map((m, i) => (
            <div
              key={m.point.t + ':' + m.point.x + ':' + m.point.y + ':' + i}
              title="Remove marker"
              className="pointer-events-auto absolute h-3 w-3 -translate-x-1/2 -translate-y-1/2 cursor-crosshair rounded-full ring-2 ring-black/60 transition-transform hover:scale-125 hover:brightness-125"
              style={{ left: `${m.point.x * 100}%`, top: `${m.point.y * 100}%`, background: m.color }}
              onClick={(e) => {
                e.stopPropagation()
                removeMarker(m.trackKey, m.point)
              }}
            />
          ))}
      </div>

      <div className="mx-auto mt-2 flex w-full max-w-[747px] items-center justify-between gap-2.5">
        <div className="flex items-center gap-2.5">
        <button
          onClick={toggle}
          aria-label={playing ? 'Pause' : 'Play'}
          className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-blue-600 text-white transition-colors hover:bg-blue-500"
        >
          {playing ? <Pause size={14} /> : <Play size={14} className="ml-0.5" />}
        </button>
        <button
          onClick={stop}
          aria-label="Stop"
          title="Stop"
          className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-white/10 text-neutral-300 transition-colors hover:bg-white/15 hover:text-white"
        >
          <Square size={11} fill="currentColor" />
        </button>
        <button
          onClick={() => setLoop((l) => !l)}
          aria-label={loop ? 'Disable loop' : 'Enable loop'}
          aria-pressed={loop}
          title={loop ? 'Loop on' : 'Loop off'}
          className={`flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full transition-colors ${
            loop
              ? 'bg-blue-600 text-white hover:bg-blue-500'
              : 'bg-white/10 text-neutral-400 hover:bg-white/15 hover:text-white'
          }`}
        >
          <Repeat size={14} />
        </button>
        <button
          onClick={() => setSettingsOpen(true)}
          aria-label="Settings"
          title="Settings"
          className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-white/10 text-neutral-300 transition-colors hover:bg-white/15 hover:text-white"
        >
          <Settings2 size={14} />
        </button>
        </div>
        <span className="text-xs tabular-nums text-neutral-400">
          {formatTime(current)}/{formatTime(duration)}
        </span>
      </div>

      <Timeline
        current={current}
        duration={duration}
        buffered={buffered}
        playing={playing}
        tracks={tracks}
        selectedKey={selectedKey}
        onSeek={seek}
        onSelectTrack={selectTrack}
        onRenameTrack={renameTrack}
        onTopicTrack={changeTrackTopic}
        onFrameIdTrack={changeTrackFrameId}
        onStampedTrack={changeTrackStamped}
        onAddTrack={addTrack}
        onRemoveTrack={removeTrack}
      />

      {settingsOpen &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setSettingsOpen(false)
            }}
            role="dialog"
            aria-modal="true"
            aria-label="Video settings"
          >
            <div className="w-full max-w-sm rounded-xl border border-white/10 bg-[#0e1116] p-5 shadow-2xl">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Video Settings</h2>
                <button
                  onClick={() => setSettingsOpen(false)}
                  aria-label="Close"
                  className="cursor-pointer rounded-md p-1 text-neutral-500 transition-colors hover:bg-white/10 hover:text-white"
                >
                  <X size={16} />
                </button>
              </div>
              <label className="block text-[13px] font-medium text-neutral-300">
                Image output
              </label>
              <input
                value={topicDraft}
                onChange={(e) => setTopicDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitPlayerTopic()
                  else if (e.key === 'Escape') setSettingsOpen(false)
                }}
                placeholder="/media_player/image"
                className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
              />
              <label className="mt-4 block text-[13px] font-medium text-neutral-300">
                Frame ID
              </label>
              <input
                value={frameIdDraft}
                onChange={(e) => setFrameIdDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitPlayerTopic()
                  else if (e.key === 'Escape') setSettingsOpen(false)
                }}
                placeholder="media_player"
                className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
              />
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setSettingsOpen(false)}
                  className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-white/10"
                >
                  Cancel
                </button>
                <button
                  onClick={commitPlayerTopic}
                  className="cursor-pointer rounded-md bg-blue-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-blue-500"
                >
                  Save
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}

// --- media page ------------------------------------------------------------

function MediaPage({ id }: { id: string }) {
  const [name, setName] = useState<string | null>(null)
  const [kind, setKind] = useState<'image' | 'video' | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [renameError, setRenameError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [deselectSignal, setDeselectSignal] = useState(0)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Resolve id -> {name, kind} from the gallery listing.
  useEffect(() => {
    let alive = true
    setLoadError(null)
    setKind(null)
    setName(null)
    ;(async () => {
      try {
        const res = await fetch('/api/media')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const body = await res.json()
        const found = (body.media ?? []).find((m: MediaItem) => m.id === id)
        if (!found) throw new Error('Media not found')
        if (alive) {
          setName(found.name)
          setKind(found.kind)
        }
      } catch (e) {
        if (alive) setLoadError(`Could not load media: ${(e as Error).message}`)
      }
    })()
    return () => {
      alive = false
    }
  }, [id])

  const startEdit = useCallback(() => {
    setDraft(name ?? '')
    setRenameError(null)
    setEditing(true)
  }, [name])

  const cancelEdit = useCallback(() => {
    setDraft(name ?? '')
    setRenameError(null)
    setEditing(false)
  }, [name])

  useEffect(() => {
    if (!editing) return
    const el = inputRef.current
    el?.focus()
    const pos = el?.value.length ?? 0
    el?.setSelectionRange(pos, pos)
  }, [editing])

  const saveRename = useCallback(async () => {
    const target = draft.trim()
    if (!target || target === name) {
      cancelEdit()
      return
    }
    setBusy(true)
    setRenameError(null)
    try {
      const res = await fetch('/api/media/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, newName: target }),
      })
      const body = (await res.json().catch(() => null)) as {
        name?: string
        error?: string
      } | null
      if (!res.ok) throw new Error(body?.error ?? `HTTP ${res.status}`)
      setName(body?.name ?? target)
      setEditing(false)
    } catch (e) {
      setRenameError(`Rename failed: ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }, [id, draft, name, cancelEdit])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      saveRename()
    } else if (e.key === 'Escape') {
      cancelEdit()
    }
  }

  const deleteItem = useCallback(async () => {
    setBusy(true)
    setDeleteError(null)
    try {
      const res = await fetch(`/api/media/${encodeURIComponent(id)}`, { method: 'DELETE' })
      const body = (await res.json().catch(() => null)) as { error?: string } | null
      if (!res.ok) throw new Error(body?.error ?? `HTTP ${res.status}`)
      window.location.href = '/'
    } catch (e) {
      setDeleteError(`Delete failed: ${(e as Error).message}`)
      setBusy(false)
    }
  }, [id])

  const backArrow = (
    <a
      href="/"
      className="-ml-1.5 flex shrink-0 cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-sm text-neutral-400 transition-colors hover:bg-white/5 hover:text-white"
      title="Back to gallery"
    >
      <ArrowLeft size={16} />
    </a>
  )

  if (loadError) {
    return (
      <main className="flex min-h-screen flex-col px-7 py-6">
        <header className="mb-4 flex items-center gap-3">
          {backArrow}
          <h1 className="truncate text-lg font-semibold">Media</h1>
        </header>
        <p className="text-sm text-red-400">{loadError}</p>
      </main>
    )
  }

  if (name == null) {
    return (
      <main className="flex min-h-screen flex-col px-7 py-6">
        <header className="mb-4 flex items-center gap-3">
          {backArrow}
          <h1 className="truncate text-lg font-semibold">…</h1>
        </header>
      </main>
    )
  }

  const isVideo = kind === 'video'

  return (
    <main
      className="flex min-h-screen flex-col px-7 py-6"
      // Clicking empty page space (outside the timeline) unselects the track.
      onClick={(e) => {
        if (e.target === e.currentTarget) setDeselectSignal((n) => n + 1)
      }}
    >
      <header className="mb-4 flex items-center gap-3">
        {backArrow}

        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            onBlur={busy ? undefined : saveRename}
            disabled={busy}
            className="min-w-0 flex-1 rounded-md border border-white/15 bg-white/5 px-2 py-1 text-lg font-semibold outline-none focus:border-blue-500"
          />
        ) : (
          <h1
            className="flex min-w-0 items-center gap-2 text-lg font-semibold"
            title={name}
          >
            <span className="truncate">{truncateName(name)}</span>
            <button
              onClick={startEdit}
              disabled={busy}
              aria-label="Rename"
              title="Rename"
              className="shrink-0 cursor-pointer rounded-md p-1 text-neutral-500 transition-colors hover:bg-white/5 hover:text-white disabled:opacity-50"
            >
              <Pencil size={15} />
            </button>
            <button
              onClick={() => {
                setDeleteError(null)
                setConfirmDelete(true)
              }}
              disabled={busy}
              aria-label="Delete"
              title="Delete"
              className="shrink-0 cursor-pointer rounded-md p-1 text-neutral-500 transition-colors hover:bg-white/5 hover:text-red-400 disabled:opacity-50"
            >
              <Trash2 size={15} />
            </button>
          </h1>
        )}
      </header>

      {renameError && <p className="mb-3 text-sm text-red-400">{renameError}</p>}

      {confirmDelete &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setConfirmDelete(false)
            }}
            role="dialog"
            aria-modal="true"
            aria-label="Delete media"
          >
            <div className="w-full max-w-sm rounded-xl border border-white/10 bg-[#0e1116] p-5 shadow-2xl">
              <div className="mb-2 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Delete media</h2>
                <button
                  onClick={() => setConfirmDelete(false)}
                  aria-label="Close"
                  className="cursor-pointer rounded-md p-1 text-neutral-500 transition-colors hover:bg-white/10 hover:text-white"
                >
                  <X size={16} />
                </button>
              </div>
              <p className="text-[13px] leading-relaxed text-neutral-300">
                Delete <span className="font-medium text-white">{truncateName(name)}</span> permanently? This also removes its timeline.
              </p>
              {deleteError && <p className="mt-3 text-sm text-red-400">{deleteError}</p>}
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setConfirmDelete(false)}
                  disabled={busy}
                  className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-neutral-300 transition-colors hover:bg-white/10 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={deleteItem}
                  disabled={busy}
                  className="cursor-pointer rounded-md bg-red-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-red-500 disabled:opacity-50"
                >
                  {busy ? 'Deleting…' : 'Delete'}
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}

      {isVideo ? (
        <VideoPlayer id={id} deselectSignal={deselectSignal} />
      ) : (
        <img
          src={mediaUrl(id)}
          alt={name}
          className="max-h-[80vh] w-auto rounded-md object-contain"
        />
      )}
    </main>
  )
}

// --- app -------------------------------------------------------------------

export default function App() {
  const path = usePath()

  if (path.startsWith(PLAYER_PREFIX)) {
    const id = decodeURIComponent(path.slice(PLAYER_PREFIX.length)).split('/')[0]
    if (id) return <MediaPage id={id} />
  }

  return <Gallery />
}
