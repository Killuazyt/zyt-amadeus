import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import type { ChatMessage } from '@/types/chat'
import type { LlmProvider } from '@/constants/providers'

const STORAGE_KEY = 'amadeus-chat-history'
const USERNAME_KEY = 'amadeus-username'
const CONFIG_KEY = 'amadeus-config'

interface AmadeusConfig {
  llmProvider: LlmProvider
  llmBaseUrl: string
  llmApiKey: string
  llmModel: string
  whisperApiKey: string
  whisperBaseUrl: string
  whisperModel: string
  ttsApiKey: string
  ttsVoiceId: string
  webrtcUrl: string
  voiceOutputLanguage: string
  textOutputLanguage: string
  systemPrompt: string
}

const DEFAULT_CONFIG: AmadeusConfig = {
  llmProvider: 'mimo',
  llmBaseUrl: 'https://token-plan-cn.xiaomimimo.com/v1',
  llmApiKey: '',
  llmModel: 'mimo-v2.5-pro',
  whisperApiKey: '',
  whisperBaseUrl: 'https://token-plan-cn.xiaomimimo.com/v1',
  whisperModel: 'mimo-v2.5-asr',
  ttsApiKey: '',
  ttsVoiceId: '冰糖',
  webrtcUrl: 'http://localhost:8001',
  voiceOutputLanguage: 'ja',
  textOutputLanguage: 'zh',
  systemPrompt: '牧瀬紅莉栖，一个天才少女科学家，性格傲娇，不喜欢被叫克里斯蒂娜',
}

interface ChatState {
  messages: ChatMessage[]
  isLoading: boolean
  isListening: boolean
  isStreaming: boolean
  streamingText: string
  username: string | null
  emotion: string | null
  motion: string | null
  config: AmadeusConfig
  addMessage: (msg: ChatMessage) => void
  setLoading: (v: boolean) => void
  setListening: (v: boolean) => void
  setUsername: (v: string) => void
  setEmotion: (v: string | null) => void
  setMotion: (v: string | null) => void
  setStreaming: (v: boolean) => void
  appendStreamingText: (text: string) => void
  finishStreaming: (finalMsg: ChatMessage) => void
  updateConfig: (patch: Partial<AmadeusConfig>) => void
  clearMessages: () => void
}

const ChatContext = createContext<ChatState | null>(null)

function loadMessages(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function loadUsername(): string | null {
  return localStorage.getItem(USERNAME_KEY)
}

function loadConfig(): AmadeusConfig {
  try {
    const raw = localStorage.getItem(CONFIG_KEY)
    return raw ? { ...DEFAULT_CONFIG, ...JSON.parse(raw) } : { ...DEFAULT_CONFIG }
  } catch {
    return { ...DEFAULT_CONFIG }
  }
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>(loadMessages)
  const [isLoading, setLoading] = useState(false)
  const [isListening, setListening] = useState(false)
  const [isStreaming, setStreaming] = useState(false)
  const [streamingText, setStreamingText] = useState('')
  const [username, setUsernameState] = useState<string | null>(loadUsername)
  const [emotion, setEmotion] = useState<string | null>(null)
  const [motion, setMotion] = useState<string | null>(null)
  const [config, setConfig] = useState<AmadeusConfig>(loadConfig)

  const addMessage = useCallback((msg: ChatMessage) => {
    setMessages((prev) => {
      const next = [...prev, msg]
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      return next
    })
  }, [])

  const clearMessages = useCallback(() => {
    setMessages([])
    localStorage.removeItem(STORAGE_KEY)
  }, [])

  const setUsername = useCallback((name: string) => {
    setUsernameState(name)
    localStorage.setItem(USERNAME_KEY, name)
  }, [])

  const appendStreamingText = useCallback((text: string) => {
    setStreamingText((prev) => prev + text)
  }, [])

  const finishStreaming = useCallback((finalMsg: ChatMessage) => {
    setStreaming(false)
    setStreamingText('')
    addMessage(finalMsg)
  }, [addMessage])

  const updateConfig = useCallback((patch: Partial<AmadeusConfig>) => {
    setConfig((prev) => {
      const next = { ...prev, ...patch }
      localStorage.setItem(CONFIG_KEY, JSON.stringify(next))
      return next
    })
  }, [])

  return (
    <ChatContext.Provider value={{
      messages, isLoading, isListening, isStreaming, streamingText,
      username, emotion, motion, config,
      addMessage, setLoading, setListening, setUsername,
      setEmotion, setMotion, setStreaming, appendStreamingText, finishStreaming,
      updateConfig, clearMessages,
    }}>
      {children}
    </ChatContext.Provider>
  )
}

export function useChat() {
  const ctx = useContext(ChatContext)
  if (!ctx) throw new Error('useChat must be used within ChatProvider')
  return ctx
}
