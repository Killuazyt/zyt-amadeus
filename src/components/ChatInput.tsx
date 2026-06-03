import { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Send, Mic } from 'lucide-react'
import { useChat } from '@/store/chatStore'
import type { ChatMessage, Emotion } from '@/types/chat'
import toast from 'react-hot-toast'

export default function ChatInput() {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const {
    addMessage, setLoading, isLoading, setEmotion, setMotion,
    isStreaming, setStreaming, appendStreamingText, finishStreaming,
    config, messages,
  } = useChat()
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`
    }
  }, [text])

  async function handleSend() {
    const content = text.trim()
    if (!content || isLoading || isStreaming) return

    setText('')

    const userMsg: ChatMessage = { role: 'user', content, timestamp: Date.now() }
    addMessage(userMsg)
    setLoading(true)
    setStreaming(true)
    setMotion('speaking')

    try {
      // Build history for multi-turn
      const history = messages.slice(-10).map((m) => ({ role: m.role, content: m.content }))

      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: content,
          history,
          apiKey: config.llmApiKey,
          baseUrl: config.llmBaseUrl,
          model: config.llmModel,
        }),
      })

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}))
        throw new Error(errData.error || `API error ${response.status}`)
      }

      // Check if response is streaming
      const contentType = response.headers.get('content-type') || ''
      if (contentType.includes('text/event-stream') || contentType.includes('text/plain')) {
        // Streaming response
        const reader = response.body?.getReader()
        const decoder = new TextDecoder()
        let fullText = ''

        if (reader) {
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            const chunk = decoder.decode(value, { stream: true })
            fullText += chunk
            appendStreamingText(chunk)
          }
        }

        // Parse emotion from the full text
        let emotionValue: Emotion = 'normal'
        let cleanText = fullText
        const emotionMatch = fullText.match(/'''(\w+)'''/)
        if (emotionMatch) {
          emotionValue = emotionMatch[1] as Emotion
          cleanText = fullText.replace(/'''\w+'''\n?/, '').trim()
        }

        setEmotion(emotionValue)
        finishStreaming({
          role: 'assistant',
          content: cleanText,
          emotion: emotionValue,
          timestamp: Date.now(),
        })
      } else {
        // JSON response
        const data = await response.json()
        const emotionValue = (data.emotion || 'normal') as Emotion
        setEmotion(emotionValue)
        finishStreaming({
          role: 'assistant',
          content: data.content,
          emotion: emotionValue,
          timestamp: Date.now(),
        })
      }
    } catch (err) {
      console.error('[ChatInput] Error:', err)
      // Mock response
      const mockEmotions: Emotion[] = ['smile', 'thinking', 'normal', 'blushing']
      const mockEmotion = mockEmotions[Math.floor(Math.random() * mockEmotions.length)]

      // Simulate streaming
      const mockText = `收到你的消息了: "${content}"。后端服务尚未连接，这是模拟回复。`
      for (let i = 0; i < mockText.length; i++) {
        await new Promise((r) => setTimeout(r, 20))
        appendStreamingText(mockText[i])
      }

      setEmotion(mockEmotion)
      finishStreaming({
        role: 'assistant',
        content: mockText,
        emotion: mockEmotion,
        timestamp: Date.now(),
      })
      toast.error('后端未连接，使用模拟回复')
    } finally {
      setLoading(false)
      setMotion(null)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="fixed bottom-0 left-0 right-0 z-20">
      <div className="max-w-2xl mx-auto px-6 pb-6">
        <div className="relative flex items-end gap-3 bg-amadeus-card/60 backdrop-blur-md border border-amadeus-border rounded-xl px-4 py-3">
          <div className="absolute top-0 left-0 w-2 h-2 border-t border-l border-primary/40 rounded-tl-xl" />
          <div className="absolute top-0 right-0 w-2 h-2 border-t border-r border-primary/40 rounded-tr-xl" />

          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.inputPlaceholder')}
            rows={1}
            className="flex-1 bg-transparent text-sm text-gray-200 placeholder-gray-500 resize-none outline-none max-h-[120px] leading-relaxed"
          />

          <div className="flex items-center gap-2 shrink-0">
            <button
              className="p-2 rounded-lg text-gray-400 hover:text-primary hover:bg-primary/10 transition-colors"
              title={t('chat.voice')}
            >
              <Mic size={18} />
            </button>
            <button
              onClick={handleSend}
              disabled={!text.trim() || isLoading || isStreaming}
              className="p-2 rounded-lg bg-primary/20 text-primary hover:bg-primary/30 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
              title={t('chat.send')}
            >
              <Send size={18} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
