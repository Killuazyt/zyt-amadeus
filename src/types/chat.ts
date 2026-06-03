export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  emotion?: Emotion
  timestamp: number
}

export type Emotion = 'normal' | 'smile' | 'blushing' | 'angry' | 'thinking' | 'sad'

export const EMOTION_MAP: Record<Emotion, string> = {
  normal: '常规',
  smile: '微笑',
  blushing: '脸红',
  angry: '生气',
  thinking: '思考',
  sad: '难过',
}
