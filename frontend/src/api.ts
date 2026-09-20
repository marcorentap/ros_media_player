// Thin, typed HTTP client for the ROS media player backend. Every network
// call the UI makes lives here so no component reaches for `fetch` directly.

import type { MediaItem, TimelineEnvelope, TimelineTrack } from './types'

async function parseOk<T>(res: Response): Promise<T> {
  const body = (await res.json().catch(() => null)) as { error?: string } | T | null
  if (!res.ok) {
    const err = (body as { error?: string } | null)?.error ?? `HTTP ${res.status}`
    throw new Error(err)
  }
  return body as T
}

const jsonHeaders = { 'Content-Type': 'application/json' }

export async function listMedia(): Promise<MediaItem[]> {
  const body = await parseOk<{ media?: MediaItem[] }>(await fetch('/api/media'))
  return Array.isArray(body.media) ? body.media : []
}

export async function uploadMedia(file: File): Promise<void> {
  const fd = new FormData()
  fd.append('files', file)
  await parseOk(await fetch('/api/media', { method: 'POST', body: fd }))
}

export async function getMediaItem(id: string): Promise<MediaItem | null> {
  const items = await listMedia()
  return items.find((m) => m.id === id) ?? null
}

export async function renameMedia(id: string, newName: string): Promise<string> {
  const body = await parseOk<{ name?: string }>(
    await fetch('/api/media/rename', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ id, newName }),
    }),
  )
  return body.name ?? newName
}

export async function deleteMedia(id: string): Promise<void> {
  await parseOk(await fetch(`/api/media/${encodeURIComponent(id)}`, { method: 'DELETE' }))
}

export async function getTimeline(id: string): Promise<TimelineEnvelope> {
  const res = await fetch(`/api/timeline/${encodeURIComponent(id)}`)
  const body = await parseOk<Partial<TimelineEnvelope>>(res)
  return {
    tracks: Array.isArray(body.tracks) ? body.tracks : [],
    topic: typeof body.topic === 'string' ? body.topic : '',
    frame_id: typeof body.frame_id === 'string' ? body.frame_id : 'media_player',
    fps: typeof body.fps === 'number' ? body.fps : 0,
    width: typeof body.width === 'number' ? body.width : 0,
    height: typeof body.height === 'number' ? body.height : 0,
  }
}

export function saveTimeline(id: string, envelope: TimelineEnvelope): void {
  console.log('[timeline save]', id, {
    tracks: envelope.tracks.length,
    topic: envelope.topic,
    frame_id: envelope.frame_id,
    fps: envelope.fps,
    width: envelope.width,
    height: envelope.height,
  })
  fetch(`/api/timeline/${encodeURIComponent(id)}`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(envelope),
  }).catch(() => {})
}

export type PreprocessResult = {
  url?: string
  preprocessed: boolean
  width: number
  height: number
  fps: number
}

export async function preprocess(
  id: string,
  width: number,
  height: number,
  fps: number,
): Promise<PreprocessResult> {
  return parseOk<PreprocessResult>(
    await fetch('/api/preprocess', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({ media_id: id, width, height, fps }),
    }),
  )
}

export type ControlCommand = 'play' | 'pause' | 'stop' | 'scrub'

export type ControlPayload = {
  media_id: string
  cmd: ControlCommand
  t: number
  width: number
  height: number
  fps: number
  topic: string
  frame_id: string
  loop: boolean
}

export function sendControl(payload: ControlPayload): void {
  // Fire-and-forget: the backend owns the publish cursor; a dropped ack is
  // fine and replayed by the next user action. Logged so the browser console
  // correlates 1:1 with the backend's control log lines.
  console.log('[control]', payload.cmd, {
    t: payload.t,
    fps: payload.fps,
    width: payload.width,
    height: payload.height,
    topic: payload.topic,
    frame_id: payload.frame_id,
    loop: payload.loop,
    media_id: payload.media_id.slice(0, 8),
  })
  fetch('/publish/control', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(payload),
  }).catch(() => {})
}

export type { TimelineTrack }