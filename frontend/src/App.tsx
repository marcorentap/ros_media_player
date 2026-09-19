import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import {
  ArrowLeft,
  Pause,
  Pencil,
  Play,
  Repeat,
  Square,
  StepBack,
  StepForward,
  Upload,
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
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

type ScrubberProps = {
  current: number
  duration: number
  buffered: number
  onSeek: (t: number) => void
}

function Scrubber({ current, duration, buffered, onSeek }: ScrubberProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<number | null>(null) // preview seconds while dragging

  const ratio = (t: number) => (duration > 0 ? Math.min(1, Math.max(0, t / duration)) : 0)
  const shown = drag ?? current

  const timeFromEvent = (e: ReactPointerEvent<HTMLDivElement>): number => {
    const el = trackRef.current!
    const rect = el.getBoundingClientRect()
    const p = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0
    return Math.min(1, Math.max(0, p)) * duration
  }

  return (
    <div className="flex w-full items-center gap-3">
      <span className="w-12 shrink-0 text-right text-xs tabular-nums text-neutral-400">
        {formatTime(shown)}
      </span>
      <div
        ref={trackRef}
        className="group relative h-1.5 flex-1 cursor-pointer rounded-full bg-white/10"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId)
          onSeek(timeFromEvent(e))
          setDrag(timeFromEvent(e))
        }}
        onPointerMove={(e) => {
          if ((e.buttons & 1) !== 0) {
            onSeek(timeFromEvent(e))
            setDrag(timeFromEvent(e))
          }
        }}
        onPointerUp={(e) => {
          onSeek(timeFromEvent(e))
          setDrag(null)
        }}
        onPointerCancel={() => setDrag(null)}
      >
        {/* buffered fill */}
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-white/15"
          style={{ width: `${ratio(buffered) * 100}%` }}
        />
        {/* played/scrubbed fill */}
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-blue-500"
          style={{ width: `${ratio(shown) * 100}%` }}
        />
        {/* thumb */}
        <div
          className="absolute top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full bg-white shadow-md group-hover:scale-110"
          style={{ left: `calc(${ratio(shown) * 100}% - 7px)` }}
        />
      </div>
      <span className="w-12 shrink-0 text-xs tabular-nums text-neutral-400">
        {formatTime(duration)}
      </span>
    </div>
  )
}

// --- video player with scrubber --------------------------------------------

function VideoPlayer({ id }: { id: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [buffered, setBuffered] = useState(0)
  const [loop, setLoop] = useState(false)

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
    const onEnded = () => setPlaying(false)

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
  }, [])

  const toggle = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) v.play().catch(() => {})
    else v.pause()
  }, [])

  const stop = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    v.pause()
    v.currentTime = 0
    setCurrent(0)
  }, [])

  // Frame duration, measured live via requestVideoFrameCallback so stepping
  // advances exactly one video frame (falls back to ~30fps before playback).
  const frameDurRef = useRef(1 / 30)
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

  const stepFrame = useCallback((dir: 1 | -1) => {
    const v = videoRef.current
    if (!v || !isFinite(v.duration)) return
    if (!v.paused) v.pause()
    const target = Math.min(v.duration, Math.max(0, v.currentTime + dir * frameDurRef.current))
    v.currentTime = target
    setCurrent(target)
  }, [])

  // Arrow keys to seek ±5s.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const v = videoRef.current
      if (!v) return
      const key = (e as KeyboardEvent).key
      if (key === 'ArrowRight') {
        e.preventDefault()
        v.currentTime = Math.min(v.duration || 0, v.currentTime + 5)
      } else if (key === 'ArrowLeft') {
        e.preventDefault()
        v.currentTime = Math.max(0, v.currentTime - 5)
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
          className="h-full w-full object-contain"
          controls={false}
          onClick={toggle}
          playsInline
        />
      </div>

      <div className="mt-3 flex items-center justify-center gap-2.5">
        <button
          onClick={() => stepFrame(-1)}
          aria-label="Step back one frame"
          title="Step back one frame"
          className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-white/10 text-neutral-300 transition-colors hover:bg-white/15 hover:text-white"
        >
          <StepBack size={14} />
        </button>
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
          onClick={() => stepFrame(1)}
          aria-label="Step forward one frame"
          title="Step forward one frame"
          className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-white/10 text-neutral-300 transition-colors hover:bg-white/15 hover:text-white"
        >
          <StepForward size={14} />
        </button>
      </div>

      <div className="mt-3">
        <Scrubber
          current={current}
          duration={duration}
          buffered={buffered}
          onSeek={seek}
        />
      </div>
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
    <main className="flex min-h-screen flex-col px-7 py-6">
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
          </h1>
        )}
      </header>

      {renameError && <p className="mb-3 text-sm text-red-400">{renameError}</p>}

      {isVideo ? (
        <VideoPlayer id={id} />
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