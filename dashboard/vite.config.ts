import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // In development the API runs on :8000; same-origin proxying keeps the refresh cookie working.
    proxy: { '/api': { target: process.env.ADAPTIRATE_API ?? 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  build: { chunkSizeWarningLimit: 900 },
})
