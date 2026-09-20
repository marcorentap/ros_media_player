import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, Pencil, Trash2 } from 'lucide-react'
import { deleteMedia, getMediaItem, renameMedia } from '../api'
import { mediaUrl } from '../lib/urls'
import { truncateName } from '../lib/format'
import { Modal } from './Modal'
import { VideoPlayer } from './VideoPlayer'

export function MediaPage({ id }: { id: string }) {
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

  // Resolve the media item by id from the gallery listing.
  useEffect(() => {
    let alive = true
    setLoadError(null)
    setKind(null)
    setName(null)
    getMediaItem(id)
      .then((found) => {
        if (!alive) return
        if (!found) throw new Error('Media not found')
        setName(found.name)
        setKind(found.kind)
      })
      .catch((e: Error) => {
        if (alive) setLoadError(`Could not load media: ${e.message}`)
      })
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
      setName(await renameMedia(id, target))
      setEditing(false)
    } catch (e) {
      setRenameError(`Rename failed: ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }, [id, draft, name, cancelEdit])

  const onKeyDown = (e: { key: string; preventDefault(): void }) => {
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
      await deleteMedia(id)
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
          <h1 className="flex min-w-0 items-center gap-2 text-lg font-semibold" title={name}>
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

      {confirmDelete && (
        <Modal title="Delete media" onClose={() => setConfirmDelete(false)}>
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
        </Modal>
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