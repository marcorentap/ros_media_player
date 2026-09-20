import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { Pause, Play, Repeat, Settings2, Square } from 'lucide-react'
import {
  getTimeline,
  preprocess,
  saveTimeline as apiSaveTimeline,
  sendControl,
} from '../api'
import type { TimelinePoint, TimelineTrack } from '../types'
import { formatTime, clamp01 } from '../lib/format'
import { DEFAULT_TRACKS, trackColor } from '../lib/tracks'
import { useLatest } from '../lib/hooks'
import { Timeline } from './Timeline'
import { Modal } from './Modal'

export function VideoPlayer({ id, deselectSignal }: { id: string; deselectSignal: number }) {
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
  // Desired publish FPS; 0 means "auto" (use the source video's measured rate).
  const [playerFps, setPlayerFps] = useState(0)
  // Desired publish dimensions; 0 means "auto" (keep the source's natural size).
  const [playerWidth, setPlayerWidth] = useState(0)
  const [playerHeight, setPlayerHeight] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [topicDraft, setTopicDraft] = useState('')
  const [frameIdDraft, setFrameIdDraft] = useState('')
  const [fpsDraft, setFpsDraft] = useState('')
  const [widthDraft, setWidthDraft] = useState('')
  const [heightDraft, setHeightDraft] = useState('')
  // The video may not play until it has been preprocessed (normalized) on the
  // backend. While that's in flight we show a spinner and leave src empty;
  // playSrc is set to the resolved stream url once ready.
  const [processing, setProcessing] = useState(true)
  const [playSrc, setPlaySrc] = useState<string | null>(null)

  // Frame duration, measured live via requestVideoFrameCallback so stepping
  // advances exactly one video frame (falls back to ~30fps before playback).
  const frameDurRef = useRef(1 / 30)
  const lastStepRef = useRef(0)
  // Debounce state for scrub publishing. Dragging the timeline fires a burst
  // of onSeek calls; each backend scrub is a one-shot decode+publish, so we
  // coalesce them to a single command once the burst settles instead of
  // hammering the backend on every pointer move.
  const scrubTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scrubPendingRef = useRef<number | null>(null)

  // The size/fps of the stream the backend resolved via /api/preprocess. This
  // is the single authority for the width/height/fps we tell the backend to
  // publish: the backend publishes *that* stream, so using its own dimensions
  // keeps the ROS frame exactly aligned with what's on screen. Deriving these
  // from the live <video> element or the browser-measured rate instead can
  // diverge from what the backend built (e.g. an AV1 source that got transcoded
  // at a different rate), which is how a frame-at-one-rate request ends up
  // decoding nothing. Zeros mean the stream hasn't resolved yet.
  const preResRef = useRef({ width: 0, height: 0, fps: 0 })

  // Live mirrors of state so `[]`-once effects (key handlers, media listeners,
  // preprocess) and stale-closure callbacks always read the current values.
  const tracksRef = useLatest(tracks)
  const topicRef = useLatest(playerTopic)
  const frameIdRef = useLatest(playerFrameId)
  const fpsRef = useLatest(playerFps)
  const widthRef = useLatest(playerWidth)
  const heightRef = useLatest(playerHeight)
  const processingRef = useLatest(processing)
  const loopRef = useLatest(loop)

  // Persist the full timeline envelope after every change. Each mutation
  // passes the tracks slice it just built; the other fields are read from the
  // latest refs so a change that touched one slice never clobbers the rest.
  const saveTimeline = useCallback(
    (next: TimelineTrack[], topic: string, frameId: string, fps: number) => {
      apiSaveTimeline(id, {
        tracks: next,
        topic,
        frame_id: frameId,
        fps,
        width: widthRef.current,
        height: heightRef.current,
      })
    },
    [id],
  )

  // Load persisted timeline state for this media item.
  useEffect(() => {
    let alive = true
    setTracks(null)
    getTimeline(id)
      .then((body) => {
        if (!alive) return
        // Default tracks only apply to new media (nothing persisted yet). Once
        // a media item has any saved timeline, use exactly what it persisted.
        setTracks(body.tracks.length ? body.tracks : DEFAULT_TRACKS)
        setSelectedKey((prev) => prev ?? (body.tracks[0]?.key ?? DEFAULT_TRACKS[0]?.key ?? null))
        setPlayerTopic(body.topic)
        setPlayerFrameId(body.frame_id)
        setPlayerFps(body.fps)
        setPlayerWidth(body.width)
        setPlayerHeight(body.height)
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

  // Set the player-level ROS topic + frame id + desired FPS and persist now.
  const changePlayerTopic = useCallback(
    (topic: string, frameId: string, fps: number, width: number, height: number) => {
      widthRef.current = width
      heightRef.current = height
      setPlayerTopic(topic)
      setPlayerFrameId(frameId)
      setPlayerFps(fps)
      setPlayerWidth(width)
      setPlayerHeight(height)
      saveTimeline(tracksRef.current ?? [], topic, frameId, fps)
    },
    [saveTimeline],
  )

  // Set a single track's ROS topic and persist.
  const changeTrackTopic = useCallback(
    (key: string, topic: string) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, topic } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  const changeTrackFrameId = useCallback(
    (key: string, frameId: string) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, frameId } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
        return next
      })
    },
    [saveTimeline],
  )

  const changeTrackStamped = useCallback(
    (key: string, stamped: boolean) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) => (t.key === key ? { ...t, stamped } : t))
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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
      setFpsDraft(playerFps > 0 ? String(playerFps) : '')
      setWidthDraft(playerWidth > 0 ? String(playerWidth) : '')
      setHeightDraft(playerHeight > 0 ? String(playerHeight) : '')
    }
  }, [settingsOpen, playerTopic, playerFrameId, playerFps, playerWidth, playerHeight])

  const commitPlayerTopic = useCallback(() => {
    // Empty / non-numeric / non-positive means "auto" (use source rate/size).
    const parsed = Number(fpsDraft)
    const fps = Number.isFinite(parsed) && parsed > 0 ? parsed : 0
    const wp = Number(widthDraft)
    const width = Number.isFinite(wp) && wp > 0 ? Math.round(wp) : 0
    const hp = Number(heightDraft)
    const height = Number.isFinite(hp) && hp > 0 ? Math.round(hp) : 0
    changePlayerTopic(topicDraft.trim(), frameIdDraft.trim(), fps, width, height)
    setSettingsOpen(false)
  }, [changePlayerTopic, topicDraft, frameIdDraft, fpsDraft, widthDraft, heightDraft])

  // Send a playback control to the backend. The backend owns the decode + ROS
  // publish cursor; the browser only says "play/pause/stop/scrub at time t".
  const send = useCallback(
    (cmd: 'play' | 'pause' | 'stop' | 'scrub', t: number) => {
      const v = videoRef.current
      // Width/height/fps come from the resolved preprocess stream (the exact
      // file the backend will decode/publish), not the live <video> element:
      // that element just displays whatever stream we handed it and its
      // measured rate only matches the backend's when the codec/rate agree.
      const pre = preResRef.current
      const w = pre.width > 0 ? pre.width : (v?.videoWidth ?? 0)
      const h = pre.height > 0 ? pre.height : (v?.videoHeight ?? 0)
      const fps = pre.fps > 0
        ? pre.fps
        : (fpsRef.current > 0
            ? fpsRef.current
            : (frameDurRef.current ? Math.round(1 / frameDurRef.current) : 30))
      sendControl({
        media_id: id,
        cmd,
        t,
        width: w,
        height: h,
        fps,
        topic: topicRef.current,
        frame_id: frameIdRef.current,
        loop: loopRef.current,
      })
    },
    [id],
  )

  // Mirror so `[]`-once effects can fire controls without a stale closure.
  const sendRef = useLatest(send)

  // Preprocess the video into a normalized offline stream (size/fps) so ROS
  // publishing never re-encodes in the live path. Fires on visit (mount) and
  // again whenever the publish FPS setting changes (a new cache key). The
  // browser is held back (spinner, no src) until this resolves so it plays the
  // same normalized stream the backend publishes, not the raw original.
  useEffect(() => {
    let alive = true
    setProcessing(true)
    setPlaySrc(null)
    // A new stream is being built; until /api/preprocess resolves there is no
    // authoritative size/fps, so clear the last resolution so a control sent
    // in this window doesn't publish against stale stream dimensions.
    preResRef.current = { width: 0, height: 0, fps: 0 }
    // A config change (fps/width/height) live-swaps the normalized stream.
    // If the node was mid-stream, stop it now so it doesn't keep publishing
    // frames at the stale settings while the new stream is being built.
    const v = videoRef.current
    sendRef.current('stop', v ? v.currentTime : 0)
    preprocess(id, playerWidth, playerHeight, fpsRef.current)
      .then((body) => {
        if (!alive) return
        // The backend resolved the stream shape; record it as authoritative
        // for future controls.
        preResRef.current = {
          width: body.width || 0,
          height: body.height || 0,
          fps: body.fps || 0,
        }
        if (typeof body.url === 'string') setPlaySrc(body.url)
        else setPlaySrc(`/media/${encodeURIComponent(id)}`)
        setProcessing(false)
      })
      .catch(() => {
        // Preprocess unavailable (no ffmpeg/probe): fall back to the original.
        if (!alive) return
        setPlaySrc(`/media/${encodeURIComponent(id)}`)
        setProcessing(false)
      })
    return () => {
      alive = false
    }
  }, [id, playerFps, playerWidth, playerHeight])

  const addMarker = useCallback(
    (key: string, point: TimelinePoint) => {
      setTracks((prev) => {
        if (!prev) return prev
        const next = prev.map((t) =>
          t.key === key ? { ...t, points: [...t.points, point] } : t,
        )
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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
          t.key === key ? { ...t, points: t.points.filter((p) => p !== point) } : t,
        )
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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
        saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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
    saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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
    saveTimeline(next, topicRef.current, frameIdRef.current, fpsRef.current)
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

  // Keep the video element's loop flag in sync with state.
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
      sendRef.current('stop', v.currentTime)
    }

    v.addEventListener('timeupdate', onTime)
    v.addEventListener('durationchange', onDur)
    v.addEventListener('play', onPlay)
    v.addEventListener('pause', onPause)
    v.addEventListener('progress', onProgress)
    v.addEventListener('ended', onEnded)
    return () => {
      if (scrubTimerRef.current) clearTimeout(scrubTimerRef.current)
      v.removeEventListener('timeupdate', onTime)
      v.removeEventListener('durationchange', onDur)
      v.removeEventListener('play', onPlay)
      v.removeEventListener('pause', onPause)
      v.removeEventListener('progress', onProgress)
      v.removeEventListener('ended', onEnded)
    }
  }, [])

  const seek = useCallback(
    (t: number) => {
      if (processingRef.current) return
      const v = videoRef.current
      if (!v || !isFinite(t)) return
      // Scrub always pauses playback: it publishes one frame at t on the
      // backend, so the video must not keep advancing past it.
      v.pause()
      v.currentTime = t
      setCurrent(t)
      // Coalesce the backend scrub: remember the latest time and fire a single
      // command once the burst of seeks settles, then a trailing one after the
      // last seek so the exact landing position is always published.
      scrubPendingRef.current = t
      if (scrubTimerRef.current) clearTimeout(scrubTimerRef.current)
      scrubTimerRef.current = setTimeout(() => {
        const te = scrubPendingRef.current
        if (te == null) return
        scrubPendingRef.current = null
        send('scrub', te)
      }, 90)
    },
    [send],
  )

  const toggle = useCallback(() => {
    if (processingRef.current) return
    const v = videoRef.current
    if (!v) return
    if (v.paused) {
      v.play().catch(() => {})
      send('play', v.currentTime)
    } else {
      v.pause()
      send('pause', v.currentTime)
    }
  }, [send])

  const stop = useCallback(() => {
    if (processingRef.current) return
    const v = videoRef.current
    if (!v) return
    v.pause()
    v.currentTime = 0
    setCurrent(0)
    send('stop', 0)
  }, [send])

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
      if (processingRef.current) return
      if (key === ' ') {
        e.preventDefault()
        if (v.paused) {
          v.play().catch(() => {})
          sendRef.current('play', v.currentTime)
        } else {
          v.pause()
          sendRef.current('pause', v.currentTime)
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
        sendRef.current('scrub', nextT)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      <div className="relative aspect-video mx-auto max-h-[420px] w-full max-w-[747px] overflow-hidden rounded-lg bg-black">
        {processing && (
          <div className="pointer-events-none absolute inset-0 z-30 flex flex-col items-center justify-center gap-2">
            <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-white/25 border-t-white" />
            <span className="text-xs text-neutral-400">Preprocessing…</span>
          </div>
        )}
        <video
          ref={videoRef}
          src={playSrc ?? undefined}
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
            onClick={() => {
              const next = !loopRef.current
              setLoop(next)
              loopRef.current = next
              // Propagate the new loop flag to a running publish stream so it
              // loops (or stops looping) at EOF instead of waiting for the next
              // play command.
              const v = videoRef.current
              if (v && !v.paused && !processingRef.current) {
                sendRef.current('play', v.currentTime)
              }
            }}
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

      {settingsOpen && (
        <Modal title="Video Settings" onClose={() => setSettingsOpen(false)}>
          <label className="block text-[13px] font-medium text-neutral-300">Image output</label>
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
          <label className="mt-4 block text-[13px] font-medium text-neutral-300">Frame ID</label>
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
          <label className="mt-4 block text-[13px] font-medium text-neutral-300">Publish FPS</label>
          <input
            value={fpsDraft}
            onChange={(e) => setFpsDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitPlayerTopic()
              else if (e.key === 'Escape') setSettingsOpen(false)
            }}
            type="number"
            min="1"
            step="1"
            placeholder="auto (source rate)"
            className="mt-1.5 w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
          />
          <label className="mt-4 block text-[13px] font-medium text-neutral-300">Publish resolution</label>
          <div className="mt-1.5 grid grid-cols-2 gap-2">
            {(
              [
                [widthDraft, setWidthDraft, 'width', 'Publish width', 'source width'],
                [heightDraft, setHeightDraft, 'height', 'Publish height', 'source height'],
              ] as const
            ).map(([val, set, name, label, hint]) => (
              <div key={name}>
                <input
                  value={val}
                  onChange={(e) => set(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitPlayerTopic()
                    else if (e.key === 'Escape') setSettingsOpen(false)
                  }}
                  inputMode="numeric"
                  type="number"
                  min="1"
                  step="1"
                  placeholder={name}
                  aria-label={label}
                  title={`Publish ${name} (0 / empty = ${hint})`}
                  className="w-full rounded-md border border-white/15 bg-white/5 px-3 py-2 text-xs text-white outline-none focus:border-blue-500"
                />
              </div>
            ))}
          </div>
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
        </Modal>
      )}
    </>
  )
}