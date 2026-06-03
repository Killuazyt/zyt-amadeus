export interface Live2DModel {
  internalModel: {
    coreModel: {
      setParameterValueById: (id: string, value: number) => void
    }
  }
}

export interface WebRTCOptions {
  apiBaseUrl?: string
  onConnected?: () => void
  onDisconnected?: () => void
  onAudioLevel?: (level: number) => void
  onAudioStream?: (stream: MediaStream) => void
  onMessage?: (message: Record<string, unknown>) => void
  onTranscript?: (text: string) => void
  onLLMResponse?: (text: string) => void
  onLLMStream?: (text: string) => void
  onEmotionResponse?: (emotion: string) => void
  onError?: (errorMessage: string) => void
  onWebrtcIdChange?: (webrtcId: string) => void
  onAudioSilence?: () => void
  onNextAction?: (action: string) => void
  onMicrophoneToggle?: (isActive: boolean) => void
  live2dModel?: Live2DModel | null
}
