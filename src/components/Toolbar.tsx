import { useTranslation } from 'react-i18next'
import { MessageSquare, Settings, Mic, MicOff, Phone, PhoneOff } from 'lucide-react'

interface Props {
  onOpenHistory: () => void
  onOpenConfig: () => void
  isConnected?: boolean
  isMicMuted?: boolean
  onToggleVoice?: () => void
  onToggleMic?: () => void
}

export default function Toolbar({
  onOpenHistory, onOpenConfig,
  isConnected, isMicMuted, onToggleVoice, onToggleMic,
}: Props) {
  const { t } = useTranslation()
  return (
    <div className="fixed right-4 top-1/2 -translate-y-1/2 z-10 flex flex-col gap-2">
      <ToolButton
        icon={isConnected ? <PhoneOff size={18} /> : <Phone size={18} />}
        label={isConnected ? t('toolbar.disconnectVoice') : t('toolbar.connectVoice')}
        onClick={onToggleVoice}
        active={isConnected}
      />
      <ToolButton
        icon={isMicMuted ? <MicOff size={18} /> : <Mic size={18} />}
        label={isMicMuted ? t('toolbar.unmute') : t('toolbar.mute')}
        onClick={onToggleMic}
        active={!isMicMuted}
        disabled={!isConnected}
      />
      <ToolButton icon={<MessageSquare size={18} />} label={t('toolbar.history')} onClick={onOpenHistory} />
      <ToolButton icon={<Settings size={18} />} label={t('toolbar.settings')} onClick={onOpenConfig} />
    </div>
  )
}

function ToolButton({
  icon, label, onClick, active, disabled,
}: {
  icon: React.ReactNode; label: string; onClick?: () => void; active?: boolean; disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      disabled={disabled}
      className={`
        group p-3 rounded-xl border backdrop-blur-sm transition-all
        ${active
          ? 'bg-primary/15 border-primary/40 text-primary'
          : 'bg-amadeus-card/40 border-amadeus-border text-gray-400 hover:text-primary hover:border-primary/30 hover:bg-primary/5'
        }
        ${disabled ? 'opacity-30 cursor-not-allowed' : ''}
      `}
    >
      {icon}
    </button>
  )
}
