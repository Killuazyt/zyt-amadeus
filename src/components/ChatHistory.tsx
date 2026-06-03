import { useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { X, Trash2 } from 'lucide-react'
import { useChat } from '@/store/chatStore'
import type { ChatMessage } from '@/types/chat'

interface Props {
  open: boolean
  onClose: () => void
}

export default function ChatHistory({ open, onClose }: Props) {
  const { t } = useTranslation()
  const { messages, clearMessages } = useChat()
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, open])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Modal */}
      <div className="relative w-full max-w-lg mx-4 h-[70vh] bg-amadeus-card border border-amadeus-border rounded-xl flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-amadeus-border">
          <h3 className="text-sm font-medium tracking-wider text-primary">{t('toolbar.history').toUpperCase()}</h3>
          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                clearMessages()
                onClose()
              }}
              className="p-1.5 rounded-lg text-gray-500 hover:text-red-400 hover:bg-red-400/10 transition-colors"
              title={t('chat.clearHistory')}
            >
              <Trash2 size={16} />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-white/5 transition-colors"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {messages.length === 0 ? (
            <p className="text-center text-gray-500 text-sm mt-20">{t('chat.noHistory')}</p>
          ) : (
            messages.map((msg, i) => <MessageBubble key={i} msg={msg} />)
          )}
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const { t } = useTranslation()
  const isUser = msg.role === 'user'
  const time = new Date(msg.timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[80%] ${isUser ? 'order-1' : 'order-1'}`}>
        {/* Role label */}
        <div className={`text-[10px] mb-1 ${isUser ? 'text-right text-gray-500' : 'text-primary/60'}`}>
          {isUser ? t('chat.you') : t('chat.amadeus')}
          {msg.emotion && msg.emotion !== 'normal' && (
            <span className="ml-2 text-primary/40">[{t(`emotions.${msg.emotion}`)}]</span>
          )}
        </div>

        {/* Bubble */}
        <div
          className={`
            px-4 py-2.5 rounded-xl text-sm leading-relaxed
            ${isUser
              ? 'bg-primary/15 text-gray-200 rounded-tr-sm'
              : 'bg-white/5 text-gray-300 rounded-tl-sm border border-amadeus-border'
            }
          `}
        >
          {msg.content}
        </div>

        {/* Time */}
        <div className={`text-[10px] text-gray-600 mt-1 ${isUser ? 'text-right' : ''}`}>
          {time}
        </div>
      </div>
    </div>
  )
}
