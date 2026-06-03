import { useTranslation } from 'react-i18next'
import { useChat } from '@/store/chatStore'

export default function DialogBox() {
  const { t } = useTranslation()
  const { messages, isLoading, isListening, isStreaming, streamingText } = useChat()

  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant')

  let display = ''
  let showCursor = false

  if (isLoading && !isStreaming) {
    display = ''
    showCursor = true
  } else if (isStreaming && streamingText) {
    display = streamingText
    showCursor = true
  } else if (isListening) {
    display = t('chat.listening')
  } else if (lastAssistant) {
    display = lastAssistant.content
  } else {
    display = t('chat.waiting')
  }

  return (
    <div className="fixed bottom-24 left-1/2 -translate-x-1/2 w-full max-w-2xl px-6 z-10">
      <div className="relative bg-amadeus-card/80 backdrop-blur-md border border-amadeus-border rounded-xl px-6 py-4 min-h-[60px]">
        {/* Corner decorations */}
        <div className="absolute top-0 left-0 w-3 h-3 border-t border-l border-primary/40 rounded-tl-xl" />
        <div className="absolute top-0 right-0 w-3 h-3 border-t border-r border-primary/40 rounded-tr-xl" />
        <div className="absolute bottom-0 left-0 w-3 h-3 border-b border-l border-primary/40 rounded-bl-xl" />
        <div className="absolute bottom-0 right-0 w-3 h-3 border-b border-r border-primary/40 rounded-br-xl" />

        {showCursor && !display ? (
          <div className="flex items-center gap-1.5 h-6">
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
          </div>
        ) : (
          <p className="text-gray-200 text-sm leading-relaxed whitespace-pre-wrap">
            {display}
            {showCursor && (
              <span className="inline-block w-0.5 h-4 bg-primary ml-0.5 animate-pulse align-middle" />
            )}
          </p>
        )}
      </div>
    </div>
  )
}
