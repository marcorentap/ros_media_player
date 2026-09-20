// Shared domain types for the media player frontend.

export type MediaItem = {
  id: string
  name: string
  mime: string
  size: number
  kind: 'image' | 'video'
}

// A marker pinned to a moment of playback. t is seconds; x,y are the click
// position normalized to the video element box (0..1) so the same dot can be
// drawn back on top of the video when the playhead passes t.
export type TimelinePoint = {
  t: number
  x: number
  y: number
}

export type TimelineTrack = {
  key: string
  name: string
  color: string
  topic: string
  frameId: string
  stamped: boolean
  points: TimelinePoint[]
}

// The full persisted timeline state: per-track data plus the player-level
// publish settings the backend stores and returns on reload.
export type TimelineEnvelope = {
  tracks: TimelineTrack[]
  topic: string
  frame_id: string
  fps: number
  width: number
  height: number
}