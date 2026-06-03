import { useState } from 'react'
import { useChat } from '@/store/chatStore'
import ParticleBackground from '@/components/ParticleBackground'
import Live2dModel from '@/components/Live2dModel'
import DialogBox from '@/components/DialogBox'
import ChatInput from '@/components/ChatInput'
import ChatHistory from '@/components/ChatHistory'
import ConfigPanel from '@/components/ConfigPanel'
import Toolbar from '@/components/Toolbar'
import LoginOverlay from '@/components/LoginOverlay'

export default function Home() {
  const { username, setUsername, emotion, motion } = useChat()
  const [historyOpen, setHistoryOpen] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [voiceConnected, setVoiceConnected] = useState(false)
  const [micMuted, setMicMuted] = useState(false)
  const isLoggedIn = username !== null

  return (
    <div className="relative h-screen flex flex-col overflow-hidden">
      <ParticleBackground />
      {!isLoggedIn && (
        <LoginOverlay onLogin={setUsername} />
      )}
      {isLoggedIn && (
        <>
          <Live2dModel
            role="牧濑红莉栖"
            emotion={emotion}
            motion={motion}
          />
          <Toolbar
            onOpenHistory={() => setHistoryOpen(true)}
            onOpenConfig={() => setConfigOpen(true)}
            isConnected={voiceConnected}
            isMicMuted={micMuted}
            onToggleVoice={() => setVoiceConnected(!voiceConnected)}
            onToggleMic={() => setMicMuted(!micMuted)}
          />
          <DialogBox />
          <ChatInput />
          <ChatHistory open={historyOpen} onClose={() => setHistoryOpen(false)} />
          <ConfigPanel open={configOpen} onClose={() => setConfigOpen(false)} />
        </>
      )}
    </div>
  )
}
