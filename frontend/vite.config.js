import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5175,
    proxy: {
      // ws: true also relays the voice websockets (/api/voice/tts, /api/voice/stt-stream)
      '/api': { target: 'http://127.0.0.1:8902', ws: true, changeOrigin: true },
    },
  },
})
