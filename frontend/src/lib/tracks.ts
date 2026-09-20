// Track color palette + timeline geometry constants.

import type { TimelineTrack } from '../types'

// Kelly's "Twenty-Two Colors of Maximum Contrast" (Kelly, 1965), as published
// and implemented by the R `Polychrome` package. The full 22-color set includes
// Black (#222222) and White (#f2f3f4); both are dropped here, leaving the 20
// chromatic colors in Kelly's original optimal-contrast ordering. With 20
// entries the cycles rarely wrap, but when they do each full cycle darkens the
// color toward black to keep repeats identifiable against the dark bg.
export const TRACK_COLORS = [
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
export const DEFAULT_TRACKS: TimelineTrack[] = [
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
export function trackColor(index: number): string {
  const base = TRACK_COLORS[index % TRACK_COLORS.length]
  const cycle = Math.floor(index / TRACK_COLORS.length)
  return cycle === 0
    ? base
    : darkenHex(base, Math.min(cycle, 3) * TRACK_COLOR_DARKEN_PER_CYCLE)
}

export const LANE_H = 32 // px height shared by every lane row for column alignment
export const RULER_H = 18 // px height of the time ruler above the lanes

// Zoom (pixels per second) limits for the scrollable timeline. The upper bound
// is high enough that the finest sub-second grid step (10 ms) can be selected.
export const ZOOM_MIN_PPS = 5
export const ZOOM_MAX_PPS = 2600