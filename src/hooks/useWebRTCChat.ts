import { useEffect, useRef, useCallback } from 'react'
import { useWebRTC } from './useWebRTC'
import { useChat } from '@/store/chatStore'
import type { ChatMessage, Emotion } from '@/types/chat'

/**
 * Bridges useWebRTC with the chat store.
 * Handles: transcript → user message, llm_stream → streaming, emotion → Live2D
 */
export function useWebRTCChat() {
  const {
    addMessage, setEmotion, setMotion, setListening,
    setStreaming, appendStreamingText, finishStreaming,
    setLoading, isStreaming,
  } = useChat()

  const streamBufferRef = useRef('')

  const webrtc = useWebRTC({
    apiBaseUrl: '/api',
    onConnected: () => {
      console.log('[WebRTC Chat] Connected')
    },
    onDisconnected: () => {
      console.log('[WebRTC Chat] Disconnected')
      setListening(false)
      setLoading(false)
      setStreaming(false)
      setMotion(null)
    },
    onTranscript: (text) => {
      // User speech recognized
      const userMsg: ChatMessage = {
        role: 'user',
        content: text,
        timestamp: Date.now(),
      }
      addMessage(userMsg)
      setListening(false)
      setLoading(true)
      setStreaming(true)
      setMotion('speaking')
      streamBufferRef.current = ''
    },
    onLLMStream: (chunk) => {
      // Streaming LLM response
      streamBufferRef.current += chunk
      appendStreamingText(chunk)
    },
    onLLMResponse: (text) => {
      // Final LLM response (if not streaming)
      if (!streamBufferRef.current) {
        const emotionMatch = text.match(/'''(\w+)'''/)
        const emotionValue = (emotionMatch ? emotionMatch[1] : 'normal') as Emotion
        const cleanText = text.replace(/'''\w+'''\n?/, '').trim()

        setEmotion(emotionValue)
        finishStreaming({
          role: 'assistant',
          content: cleanText,
          emotion: emotionValue,
          timestamp: Date.now(),
        })
      }
      setLoading(false)
      setMotion(null)
    },
    onEmotionResponse: (emotion) => {
      setEmotion(emotion)
    },
    onAudioSilence: () => {
      // User stopped speaking, AI should respond
      setListening(true)
    },
    onError: (err) => {
      console.error('[WebRTC Chat] Error:', err)
      setLoading(false)
      setStreaming(false)
    },
    onNextAction: (action) => {
      console.log('[WebRTC Chat] Next action:', action)
    },
  })

  // When streaming ends (detected by silence after response)
  useEffect(() => {
    if (!webrtc.isConnected && isStreaming) {
      // Connection lost during streaming, finalize
      if (streamBufferRef.current) {
        const emotionMatch = streamBufferRef.current.match(/'''(\w+)'''/)
        const emotionValue = (emotionMatch ? emotionMatch[1] : 'normal') as Emotion
        const cleanText = streamBufferRef.current.replace(/'''\w+'''\n?/, '').trim()

        finishStreaming({
          role: 'assistant',
          content: cleanText,
          emotion: emotionValue,
          timestamp: Date.now(),
        })
        streamBufferRef.current = ''
      }
    }
  }, [webrtc.isConnected, isStreaming, finishStreaming])

  const connectVoice = useCallback(async () => {
    try {
      await webrtc.connect()
    } catch (err) {
      console.error('[WebRTC Chat] Connect failed:', err)
    }
  }, [webrtc])

  const disconnectVoice = useCallback(() => {
    webrtc.disconnect()
  }, [webrtc])

  return {
    ...webrtc,
    connectVoice,
    disconnectVoice,
  }
}
