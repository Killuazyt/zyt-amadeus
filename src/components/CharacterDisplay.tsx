import { useState } from 'react'
import { useChat } from '@/store/chatStore'
import type { Emotion } from '@/types/chat'

const EMOTION_EMOJI: Record<Emotion, string> = {
  normal: '😐',
  smile: '😊',
  blushing: '😳',
  angry: '😠',
  thinking: '🤔',
  sad: '😢',
}

export default function CharacterDisplay() {
  const { messages } = useChat()
  const [hovering, setHovering] = useState(false)

  const lastEmotion = messages.length > 0
    ? (messages[messages.length - 1].emotion ?? 'normal')
    : 'normal'

  return (
    <div
      className="relative flex flex-col items-center justify-center flex-1 select-none"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      {/* Character placeholder - replace with Live2D or image */}
      <div className="relative">
        <div className={`
          w-48 h-48 rounded-full border-2 border-primary/30
          bg-gradient-to-br from-primary/10 to-transparent
          flex items-center justify-center
          transition-all duration-500
          ${hovering ? 'border-primary/60 shadow-lg shadow-primary/20' : ''}
        `}>
          <span className="text-6xl">{EMOTION_EMOJI[lastEmotion]}</span>
        </div>

        {/* Pulse ring */}
        <div className="absolute inset-0 rounded-full border border-primary/20 animate-ping" style={{ animationDuration: '3s' }} />
      </div>

      {/* Name tag */}
      <div className="mt-6 text-center">
        <h2 className="text-xl font-light tracking-widest text-primary">AMADEUS</h2>
        <p className="text-xs text-gray-500 mt-1 tracking-wider">牧瀬紅莉栖</p>
      </div>
    </div>
  )
}
