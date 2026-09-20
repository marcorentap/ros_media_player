import { useEffect, useRef, useState } from 'react'
import { Settings2 } from 'lucide-react'
import { LANE_H } from '../lib/tracks'
import { Modal } from './Modal'
import type { TimelineTrack } from '../types'

export function TrackRowHeading({
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
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: track.color }} />
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

      {open && (
        <Modal title="Track Settings" onClose={() => setOpen(false)}>
          <label className="block text-[13px] font-medium text-neutral-300">Name</label>
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
          <label className="mt-4 block text-[13px] font-medium text-neutral-300">ROS topic</label>
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
          <label className="mt-4 block text-[13px] font-medium text-neutral-300">Frame ID</label>
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
        </Modal>
      )}
    </div>
  )
}