import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'

interface Props {
  onLogin: (username: string) => void
}

export default function LoginOverlay({ onLogin }: Props) {
  const { t } = useTranslation()
  const [username, setUsername] = useState('')
  const [isVisible, setIsVisible] = useState(true)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  // Digital rain effect
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    canvas.width = window.innerWidth
    canvas.height = window.innerHeight

    const chars = '01アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン'
    const fontSize = 14
    const columns = Math.floor(canvas.width / fontSize)
    const drops: number[] = Array(columns).fill(1)

    let animId: number

    function draw() {
      ctx!.fillStyle = 'rgba(10, 14, 23, 0.05)'
      ctx!.fillRect(0, 0, canvas!.width, canvas!.height)

      ctx!.fillStyle = 'rgba(0, 188, 212, 0.15)'
      ctx!.font = `${fontSize}px monospace`

      for (let i = 0; i < drops.length; i++) {
        const text = chars[Math.floor(Math.random() * chars.length)]
        ctx!.fillText(text, i * fontSize, drops[i] * fontSize)
        if (drops[i] * fontSize > canvas!.height && Math.random() > 0.975) {
          drops[i] = 0
        }
        drops[i]++
      }

      animId = requestAnimationFrame(draw)
    }

    draw()

    const handleResize = () => {
      canvas!.width = window.innerWidth
      canvas!.height = window.innerHeight
    }
    window.addEventListener('resize', handleResize)

    return () => {
      cancelAnimationFrame(animId)
      window.removeEventListener('resize', handleResize)
    }
  }, [])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!username.trim()) return
    setIsVisible(false)
    setTimeout(() => onLogin(username.trim()), 500)
  }

  return (
    <div
      className={`fixed inset-0 z-[100] flex items-center justify-center transition-opacity duration-500 ${
        isVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'
      }`}
    >
      {/* Digital rain background */}
      <canvas ref={canvasRef} className="absolute inset-0" />

      {/* Grid overlay */}
      <div className="absolute inset-0 opacity-5"
        style={{
          backgroundImage: `
            linear-gradient(rgba(0,188,212,0.3) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0,188,212,0.3) 1px, transparent 1px)
          `,
          backgroundSize: '50px 50px',
        }}
      />

      {/* Scan line */}
      <div className="absolute inset-0 pointer-events-none overflow-hidden">
        <div
          className="absolute w-full h-[2px] bg-primary/10 animate-scan"
          style={{ animation: 'scan 4s linear infinite' }}
        />
      </div>

      {/* Login card */}
      <div className="relative z-10 w-full max-w-md mx-4">
        {/* Corner decorations */}
        <div className="absolute -top-px -left-px w-8 h-8 border-t-2 border-l-2 border-primary/60" />
        <div className="absolute -top-px -right-px w-8 h-8 border-t-2 border-r-2 border-primary/60" />
        <div className="absolute -bottom-px -left-px w-8 h-8 border-b-2 border-l-2 border-primary/60" />
        <div className="absolute -bottom-px -right-px w-8 h-8 border-b-2 border-r-2 border-primary/60" />

        <div className="bg-amadeus-card/80 backdrop-blur-xl border border-amadeus-border p-8">
          {/* Header */}
          <div className="text-center mb-8">
            <h1 className="text-3xl font-light tracking-[0.3em] text-primary mb-2">{t('login.title')}</h1>
            <div className="h-px bg-gradient-to-r from-transparent via-primary/40 to-transparent mb-3" />
            <p className="text-xs text-gray-500 tracking-widest">{t('login.subtitle')}</p>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-6">
            <div>
              <label className="block text-[10px] text-primary/60 tracking-widest mb-2 uppercase">
                {t('login.username')}
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('login.usernamePlaceholder')}
                autoFocus
                className="w-full bg-black/30 border border-amadeus-border rounded px-4 py-3 text-sm text-gray-200 placeholder-gray-600 outline-none focus:border-primary/50 transition-colors font-mono"
              />
            </div>

            <button
              type="submit"
              disabled={!username.trim()}
              className="w-full py-3 bg-primary/10 border border-primary/30 text-primary text-sm tracking-widest hover:bg-primary/20 hover:border-primary/50 disabled:opacity-30 disabled:cursor-not-allowed transition-all uppercase"
            >
              {t('login.connect')}
            </button>
          </form>

          {/* Footer */}
          <div className="mt-6 text-center">
            <p className="text-[10px] text-gray-600 tracking-wider">
              {t('login.tagline')}
            </p>
          </div>
        </div>
      </div>

      <style>{`
        @keyframes scan {
          0% { top: -2px; }
          100% { top: 100%; }
        }
      `}</style>
    </div>
  )
}
