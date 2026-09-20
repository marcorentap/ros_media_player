// URL/path helpers and upload-type detection for the gallery.

export const PLAYER_PREFIX = '/player/'

const IMAGE_RE = /\.(jpe?g|png|gif|webp|bmp|svg)$/i
const VIDEO_RE = /\.(mp4|webm|mov|mkv|avi|ogg)$/i

export function isAllowed(name: string): boolean {
  return IMAGE_RE.test(name) || VIDEO_RE.test(name)
}

export function mediaUrl(id: string): string {
  return `/media/${id}`
}

export function playerUrl(id: string): string {
  return `/player/${id}`
}