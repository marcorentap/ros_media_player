import { clamp01 } from './format'

// Coordinate transforms for mapping between the on-screen <video> element and
// the actual rendered image content.
//
// The <video> is shown with CSS `object-contain` inside a fixed box, so an
// off-16:9 source is letterboxed: the element rect includes black bars that
// are not image. Dividing a click by the element width/height would therefore
// yield coordinates relative to the whole box, not the image. These helpers
// invert `object-contain` so clicks and markers are measured against the true
// image content. When later published, the backend scales those normalized
// image coordinates by the stream's width/height into pixel space.
//
// Both directions take the element box size (`elW x elH`, from
// getBoundingClientRect) and the image's intrinsic size (`vw x vh`, from the
// video element's videoWidth/Height — i.e. the same normalized stream the
// backend publishes).

/** CSS `object-contain` layout for content `vw x vh` inside a `elW x elH` box. */
function containLayout(elW: number, elH: number, vw: number, vh: number): {
  offX: number
  offY: number
  imgW: number
  imgH: number
  valid: boolean
} {
  if (!(elW > 0) || !(elH > 0) || !(vw > 0) || !(vh > 0)) {
    return { offX: 0, offY: 0, imgW: elW, imgH: elH, valid: false }
  }
  const scale = Math.min(elW / vw, elH / vh)
  const imgW = vw * scale
  const imgH = vh * scale
  return { offX: (elW - imgW) / 2, offY: (elH - imgH) / 2, imgW, imgH, valid: true }
}

/** Element-box normalized (0..1) coords -> image-content normalized coords.
 *  Clicks that land in a letterbox bar clamp to the nearest image edge. */
export function elementToImage(
  ex: number, ey: number, elW: number, elH: number, vw: number, vh: number,
): { x: number; y: number } {
  const l = containLayout(elW, elH, vw, vh)
  if (!l.valid) return { x: clamp01(ex), y: clamp01(ey) }
  return {
    x: Math.max(0, Math.min(1, (ex * elW - l.offX) / l.imgW)),
    y: Math.max(0, Math.min(1, (ey * elH - l.offY) / l.imgH)),
  }
}

/** Image-content normalized (0..1) coords -> element-box normalized coords
 *  (for placing overlay dots back on top of the same rendered image). */
export function imageToElement(
  nx: number, ny: number, elW: number, elH: number, vw: number, vh: number,
): { x: number; y: number } {
  const l = containLayout(elW, elH, vw, vh)
  if (!l.valid) return { x: nx, y: ny }
  return { x: (l.offX + nx * l.imgW) / elW, y: (l.offY + ny * l.imgH) / elH }
}