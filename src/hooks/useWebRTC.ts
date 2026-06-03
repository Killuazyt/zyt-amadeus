import { useState, useEffect, useCallback, useRef } from 'react'
import type { WebRTCOptions, Live2DModel } from './types'

interface WebRTCState {
  isConnected: boolean
  isAudioActive: boolean
  audioLevel: number
  transcript: string
  llmResponse: string
  error: string | null
  webrtcId: string | null
  isMicrophoneMuted: boolean
}

interface UseWebRTCReturn extends WebRTCState {
  connect: () => Promise<void>
  disconnect: () => void
  toggleMicrophone: () => void
}

class WebRTCClient {
  private peerConnection: RTCPeerConnection | null = null
  private mediaStream: MediaStream | null = null
  private dataChannel: RTCDataChannel | null = null
  private options: WebRTCOptions
  private audioContext: AudioContext | null = null
  private analyser: AnalyserNode | null = null
  private dataArray: Uint8Array | null = null
  private animationFrameId: number | null = null
  private webrtcId: string | null = null
  private eventSource: EventSource | null = null
  private apiBaseUrl: string

  // Silence detection
  private silenceThreshold = 0.01
  private silenceStartTime: number | null = null
  private silenceDuration = 2000
  private isSilent = false

  // Live2D mouth animation
  private lastMouthOpenY = 0
  private smoothingFactor = 0.3
  private live2dModel: Live2DModel | null = null

  private isMicrophoneMuted = false

  constructor(options: WebRTCOptions = {}) {
    this.options = options
    this.apiBaseUrl = options.apiBaseUrl || '/api'
    if (options.live2dModel) {
      this.live2dModel = options.live2dModel
    }
  }

  getWebrtcId(): string | null { return this.webrtcId }
  getMicrophoneState(): boolean { return !this.isMicrophoneMuted }

  toggleMicrophone(): boolean {
    if (!this.mediaStream) return false
    const audioTracks = this.mediaStream.getAudioTracks()
    if (audioTracks.length === 0) return false
    this.isMicrophoneMuted = !this.isMicrophoneMuted
    audioTracks.forEach(track => { track.enabled = !this.isMicrophoneMuted })
    this.options.onMicrophoneToggle?.(!this.isMicrophoneMuted)
    return true
  }

  async connect() {
    try {
      // Fetch ICE config from server
      const iceConfigResponse = await fetch(`${this.apiBaseUrl}/webrtc/ice-config`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
      const iceConfig = await iceConfigResponse.json()

      this.peerConnection = new RTCPeerConnection(iceConfig)

      this.peerConnection.addEventListener('iceconnectionstatechange', () => {
        console.log('[WebRTC] ICE state:', this.peerConnection?.iceConnectionState)
      })

      this.webrtcId = Math.random().toString(36).substring(7)
      localStorage.setItem('webrtc_id', this.webrtcId)
      this.options.onWebrtcIdChange?.(this.webrtcId)

      // Get user audio
      this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      this.setupAudioAnalysis()
      this.mediaStream.getTracks().forEach(track => {
        this.peerConnection!.addTrack(track, this.mediaStream!)
      })

      // Handle incoming audio
      this.peerConnection.addEventListener('track', (event) => {
        this.options.onAudioStream?.(event.streams[0])
        if (this.live2dModel) {
          this.setupLive2dMouthMovement(event.streams[0])
        }
      })

      // Data channel for text messages
      this.dataChannel = this.peerConnection.createDataChannel('text')
      this.dataChannel.addEventListener('message', (event) => {
        const message = JSON.parse(event.data)
        this.options.onMessage?.(message)
      })

      // Create and send offer
      const offer = await this.peerConnection.createOffer()
      await this.peerConnection.setLocalDescription(offer)

      const response = await fetch(`${this.apiBaseUrl}/webrtc/offer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ sdp: offer.sdp, type: offer.type, webrtc_id: this.webrtcId }),
      })
      const serverResponse = await response.json()
      await this.peerConnection.setRemoteDescription(serverResponse)

      // Connect to SSE event stream
      this.connectToEventStream()
      this.options.onConnected?.()
    } catch (error) {
      this.disconnect()
      throw error
    }
  }

  private setupAudioAnalysis() {
    if (!this.mediaStream) return
    this.audioContext = new AudioContext()
    const source = this.audioContext.createMediaStreamSource(this.mediaStream)
    this.analyser = this.audioContext.createAnalyser()
    this.analyser.fftSize = 256
    source.connect(this.analyser)
    this.dataArray = new Uint8Array(this.analyser.frequencyBinCount)
    this.startAnalysis()
  }

  private startAnalysis() {
    const analyze = () => {
      if (!this.analyser || !this.dataArray) return
      this.analyser.getByteFrequencyData(this.dataArray as unknown as Uint8Array<ArrayBuffer>)

      // Calculate audio level
      let sum = 0
      for (let i = 0; i < this.dataArray.length; i++) {
        sum += this.dataArray[i]
      }
      const average = sum / this.dataArray.length / 255
      this.options.onAudioLevel?.(average)

      // Silence detection
      if (average < this.silenceThreshold) {
        if (!this.silenceStartTime) {
          this.silenceStartTime = Date.now()
        } else if (Date.now() - this.silenceStartTime > this.silenceDuration && !this.isSilent) {
          this.isSilent = true
          this.options.onAudioSilence?.()
        }
      } else {
        this.silenceStartTime = null
        this.isSilent = false
      }

      this.animationFrameId = requestAnimationFrame(analyze)
    }
    analyze()
  }

  private setupLive2dMouthMovement(stream: MediaStream) {
    if (!this.audioContext) return
    const source = this.audioContext.createMediaStreamSource(stream)
    const analyser = this.audioContext.createAnalyser()
    analyser.fftSize = 256
    source.connect(analyser)
    const dataArray = new Uint8Array(analyser.frequencyBinCount)

    const animate = () => {
      analyser.getByteFrequencyData(dataArray)

      let sum = 0
      for (let i = 0; i < dataArray.length; i++) {
        sum += dataArray[i]
      }
      const average = sum / dataArray.length / 255

      // Smooth the value
      const target = Math.min(average * 3, 1)
      this.lastMouthOpenY += (target - this.lastMouthOpenY) * this.smoothingFactor

      this.live2dModel?.internalModel.coreModel.setParameterValueById('ParamMouthOpenY', this.lastMouthOpenY)

      requestAnimationFrame(animate)
    }
    animate()
  }

  private connectToEventStream() {
    if (!this.webrtcId) return
    this.eventSource = new EventSource(`${this.apiBaseUrl}/events?webrtc_id=${this.webrtcId}`)

    this.eventSource.addEventListener('message', (event) => {
      try {
        const data = JSON.parse(event.data)
        switch (data.type) {
          case 'transcript':
            this.options.onTranscript?.(data.data)
            break
          case 'llm_response':
            this.options.onLLMResponse?.(data.data)
            break
          case 'llm_stream':
            this.options.onLLMStream?.(data.data)
            break
          case 'emotion_response':
            this.options.onEmotionResponse?.(data.data)
            break
          case 'next_action':
            this.options.onNextAction?.(data.data)
            break
          case 'error':
            this.options.onError?.(data.data)
            break
          default:
            this.options.onMessage?.(data)
        }
      } catch {
        console.error('[WebRTC] Failed to parse event data:', event.data)
      }
    })

    this.eventSource.addEventListener('error', () => {
      this.options.onError?.('Event stream disconnected')
    })
  }

  private stopAnalysis() {
    if (this.animationFrameId) {
      cancelAnimationFrame(this.animationFrameId)
      this.animationFrameId = null
    }
  }

  disconnect() {
    this.stopAnalysis()

    if (this.eventSource) {
      this.eventSource.close()
      this.eventSource = null
    }

    if (this.dataChannel) {
      this.dataChannel.close()
      this.dataChannel = null
    }

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach(track => track.stop())
      this.mediaStream = null
    }

    if (this.peerConnection) {
      this.peerConnection.close()
      this.peerConnection = null
    }

    if (this.audioContext) {
      this.audioContext.close()
      this.audioContext = null
    }

    this.analyser = null
    this.dataArray = null
    this.live2dModel = null
    this.options.onDisconnected?.()
  }
}

export function useWebRTC(options: WebRTCOptions = {}): UseWebRTCReturn {
  const [state, setState] = useState<WebRTCState>({
    isConnected: false,
    isAudioActive: false,
    audioLevel: 0,
    transcript: '',
    llmResponse: '',
    error: null,
    webrtcId: null,
    isMicrophoneMuted: false,
  })

  const webrtcRef = useRef<WebRTCClient | null>(null)
  const optionsRef = useRef(options)

  useEffect(() => { optionsRef.current = options }, [options])

  useEffect(() => {
    const client = new WebRTCClient({
      ...optionsRef.current,
      live2dModel: optionsRef.current.live2dModel || undefined,
      onConnected: () => {
        setState(prev => ({ ...prev, isConnected: true, webrtcId: client.getWebrtcId() }))
        optionsRef.current.onConnected?.()
      },
      onDisconnected: () => {
        setState(prev => ({ ...prev, isConnected: false, isAudioActive: false, webrtcId: null }))
        optionsRef.current.onDisconnected?.()
      },
      onAudioLevel: (level) => {
        setState(prev => ({ ...prev, audioLevel: level }))
        optionsRef.current.onAudioLevel?.(level)
      },
      onAudioStream: (stream) => {
        setState(prev => ({ ...prev, isAudioActive: true }))
        optionsRef.current.onAudioStream?.(stream)
      },
      onTranscript: (text) => {
        setState(prev => ({ ...prev, transcript: text }))
        optionsRef.current.onTranscript?.(text)
      },
      onLLMResponse: (text) => {
        setState(prev => ({ ...prev, llmResponse: text }))
        optionsRef.current.onLLMResponse?.(text)
      },
      onLLMStream: (text) => {
        setState(prev => ({ ...prev, llmResponse: prev.llmResponse + text }))
        optionsRef.current.onLLMStream?.(text)
      },
      onEmotionResponse: (emotion) => {
        optionsRef.current.onEmotionResponse?.(emotion)
      },
      onError: (msg) => {
        setState(prev => ({ ...prev, error: msg }))
        optionsRef.current.onError?.(msg)
      },
      onWebrtcIdChange: (id) => {
        setState(prev => ({ ...prev, webrtcId: id }))
        optionsRef.current.onWebrtcIdChange?.(id)
      },
      onAudioSilence: () => optionsRef.current.onAudioSilence?.(),
      onNextAction: (action) => optionsRef.current.onNextAction?.(action),
      onMicrophoneToggle: (active) => {
        setState(prev => ({ ...prev, isMicrophoneMuted: !active }))
        optionsRef.current.onMicrophoneToggle?.(active)
      },
    })

    webrtcRef.current = client
    return () => { client.disconnect(); webrtcRef.current = null }
  }, [])

  const connect = useCallback(async () => {
    if (webrtcRef.current) {
      setState(prev => ({ ...prev, error: null }))
      await webrtcRef.current.connect()
    }
  }, [])

  const disconnect = useCallback(() => {
    webrtcRef.current?.disconnect()
  }, [])

  const toggleMicrophone = useCallback(() => {
    return webrtcRef.current?.toggleMicrophone() ?? false
  }, [])

  return { ...state, connect, disconnect, toggleMicrophone }
}
