import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The ROS2 Python backend listens on :8080 during dev. Proxy API + publish
// calls there so the page talks to ROS over HTTP while Vite gives live reload.
const BACKEND = process.env.MEDIA_PLAYER_BACKEND ?? 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/media': { target: BACKEND, changeOrigin: true },
      '/publish': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})