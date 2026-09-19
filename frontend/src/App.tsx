import { useCallback, useEffect, useRef, useState } from 'react'
import { Upload } from 'lucide-react'

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

let dragDepth = 0

export default function App() {
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
          className="flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-white transition-colors hover:bg-(--btn-bg-hover) disabled:opacity-60"
          style={{ backgroundColor: 'var(--btn-bg)' }}
        >
          <Upload size={16} />
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

      <div
        className="grid grid-cols-2 flex-1 content-start items-start gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8"
      >
        {items.length === 0 && !uploading && (
          <p className="col-span-full py-16 text-center text-sm text-neutral-500">
            Drop pictures or videos here to upload.
          </p>
        )}

        {items.map((item) => (
          <figure
            key={item.id}
            className="relative m-0"
          >
            <a
              href={mediaUrl(item.id)}
              target="_blank"
              rel="noopener noreferrer"
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

        {uploading && (
          <p className="col-span-full py-4 text-center text-sm text-blue-400">Uploading…</p>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    </main>
  )
}