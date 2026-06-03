import { serve } from '@hono/node-server'
import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { chatHandler } from './chat'
import { createServer } from 'http'
import { createProxyMiddleware } from 'http-proxy-middleware'
import 'dotenv/config'

const app = new Hono()

app.use('/*', cors())

app.get('/api/health', (c) => {
  return c.json({ status: 'ok', message: 'Amadeus System is running' })
})

app.post('/api/chat', chatHandler)

const port = Number(process.env.PORT) || 3002
const WEBRTC_API_URL = process.env.WEBRTC_API_URL || 'http://localhost:8001'

console.log(`Amadeus Service running on http://localhost:${port}`)
console.log(`WebRTC API proxy: ${WEBRTC_API_URL}`)

// Create HTTP server with proxy support
const server = createServer(async (req, res) => {
  const url = new URL(req.url!, `http://${req.headers.host}`)

  // Proxy /api/webrtc/*, /api/events, /api/input_hook, etc. to Python WebRTC service
  if (
    url.pathname.startsWith('/api/webrtc') ||
    url.pathname.startsWith('/api/events') ||
    url.pathname.startsWith('/api/input_hook') ||
    url.pathname.startsWith('/api/use_builtin_service') ||
    url.pathname.startsWith('/api/reset') ||
    url.pathname.startsWith('/api/ai-trigger') ||
    url.pathname.startsWith('/api/camera-state') ||
    url.pathname.startsWith('/api/video-frame')
  ) {
    // Proxy to WebRTC service
    const targetUrl = `${WEBRTC_API_URL}${req.url}`

    try {
      const headers: Record<string, string> = {}
      for (const [key, value] of Object.entries(req.headers)) {
        if (value && typeof value === 'string') {
          headers[key] = value
        }
      }

      const proxyRes = await fetch(targetUrl, {
        method: req.method,
        headers,
        body: req.method !== 'GET' && req.method !== 'HEAD' ? req : undefined,
      })

      res.statusCode = proxyRes.status
      proxyRes.headers.forEach((value, key) => {
        res.setHeader(key, value)
      })

      if (proxyRes.body) {
        const reader = proxyRes.body.getReader()
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          res.write(value)
        }
      }
      res.end()
    } catch (err: any) {
      console.error('Proxy error:', err.message)
      res.statusCode = 502
      res.end(JSON.stringify({ error: 'WebRTC service unavailable' }))
    }
    return
  }

  // All other /api routes go to Hono
  const response = await app.fetch(new Request(url.toString(), {
    method: req.method,
    headers: req.headers as any,
    body: req.method !== 'GET' && req.method !== 'HEAD' ? req : undefined,
    duplex: 'half',
  }))

  res.statusCode = response.status
  response.headers.forEach((value, key) => res.setHeader(key, value))

  if (response.body) {
    const reader = response.body.getReader()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      res.write(value)
    }
  }
  res.end()
})

server.listen(port, () => {
  console.log(`HTTP server listening on port ${port}`)
})
