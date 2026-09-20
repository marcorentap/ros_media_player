import { Gallery } from './components/Gallery'
import { MediaPage } from './components/MediaPage'
import { PLAYER_PREFIX } from './lib/urls'
import { usePath } from './lib/hooks'

// Tiny client-side path router (no dependency): gallery at '/', player page at
// '/player/:id'. Kept intentionally small — adding routes means adding one
// `if` branch here.
export default function App() {
  const path = usePath()

  if (path.startsWith(PLAYER_PREFIX)) {
    const id = decodeURIComponent(path.slice(PLAYER_PREFIX.length)).split('/')[0]
    if (id) return <MediaPage id={id} />
  }

  return <Gallery />
}