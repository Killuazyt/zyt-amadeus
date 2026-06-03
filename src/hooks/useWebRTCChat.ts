import { useEffect, useRef, useCallback } from 'react'
import { useWebRTC } from './useWebRTC'
import { useChat } from '@/store/chatStore'
import type { ChatMessage, Emotion } from '@/types/chat'
import toast from 'react-hot-toast'

/**
 * Bridges useWebRTC with the chat store.
 * Handles: transcript → user message, llm_stream → streaming, emotion → Live2D
 */
export function useWebRTCChat() {
  const {
    addMessage, setEmotion, setMotion, setListening,
    setStreaming, appendStreamingText, finishStreaming,
    setLoading, isStreaming, config, username,
  } = useChat()

  const streamBufferRef = useRef('')
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null)

  const finishBufferedResponse = useCallback((emotion: Emotion = 'normal') => {
    const text = streamBufferRef.current
    if (!text) return

    const emotionMatch = text.match(/'''(\w+)'''/)
    const emotionValue = (emotionMatch ? emotionMatch[1] : emotion) as Emotion
    const cleanText = text.replace(/'''\w+'''\n?/, '').trim()

    setEmotion(emotionValue)
    finishStreaming({
      role: 'assistant',
      content: cleanText,
      emotion: emotionValue,
      timestamp: Date.now(),
    })

    streamBufferRef.current = ''
    setLoading(false)
    setMotion(null)
  }, [finishStreaming, setEmotion, setLoading, setMotion])

  const sendConfigToServer = useCallback(async (webrtcId: string) => {
    const response = await fetch('/api/input_hook', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        webrtc_id: webrtcId,
        llm_api_key: config.llmApiKey,
        whisper_api_key: config.whisperApiKey,
        llm_base_url: config.llmBaseUrl,
        whisper_base_url: config.whisperBaseUrl,
        whisper_model: config.whisperModel,
        ai_model: config.llmModel,
        tts_api_key: config.ttsApiKey,
        tts_voice_id: config.ttsVoiceId,
        voice_output_language: config.voiceOutputLanguage,
        text_output_language: config.textOutputLanguage,
        system_prompt: config.systemPrompt,
        user_name: username || '用户',
      }),
    })

    if (!response.ok) {
      throw new Error(`WebRTC config failed: ${response.status}`)
    }
  }, [config, username])

  const webrtc = useWebRTC({
    apiBaseUrl: '/api',
    onConnected: () => {
      console.log('[WebRTC Chat] Connected')
      setListening(true)
      toast.success('语音已连接')
    },
    onDisconnected: () => {
      console.log('[WebRTC Chat] Disconnected')
      setListening(false)
      setLoading(false)
      setStreaming(false)
      setMotion(null)
      streamBufferRef.current = ''
      if (remoteAudioRef.current) {
        remoteAudioRef.current.pause()
        remoteAudioRef.current.srcObject = null
        remoteAudioRef.current = null
      }
    },
    onWebrtcIdChange: (webrtcId) => {
      sendConfigToServer(webrtcId).catch((err) => {
        console.error('[WebRTC Chat] Config sync failed:', err)
        toast.error('语音配置同步失败')
      })
    },
    onAudioStream: (stream) => {
      const audio = remoteAudioRef.current ?? new Audio()
      audio.autoplay = true
      audio.srcObject = stream
      remoteAudioRef.current = audio
      audio.play().catch((err) => {
        console.warn('[WebRTC Chat] Remote audio playback blocked:', err)
      })
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
      finishBufferedResponse(emotion as Emotion)
    },
    onAudioSilence: () => {
      // User stopped speaking, AI should respond
      setListening(true)
    },
    onError: (err) => {
      console.error('[WebRTC Chat] Error:', err)
      setLoading(false)
      setStreaming(false)
      setListening(false)
      toast.error(err)
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
      const message = err instanceof Error ? err.message : '语音连接失败'
      toast.error(message)
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
