import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import { useChat } from '@/store/chatStore'
import { PROVIDER_PRESETS, type LlmProvider } from '@/constants/providers'

interface Props {
  open: boolean
  onClose: () => void
}

export default function ConfigPanel({ open, onClose }: Props) {
  const { t, i18n } = useTranslation()
  const { config, updateConfig } = useChat()

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      {/* Panel */}
      <div className="relative w-80 h-full bg-amadeus-card border-r border-amadeus-border flex flex-col overflow-hidden animate-slide-in-left">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-amadeus-border">
          <h3 className="text-sm font-medium tracking-wider text-primary">{t('settings.title')}</h3>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-white/5 transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
          {/* LLM Settings */}
          <Section title={t('settings.llm')}>
            <SelectField
              label={t('settings.provider')}
              value={config.llmProvider}
              onChange={(v) => {
                const provider = v as LlmProvider
                const preset = PROVIDER_PRESETS[provider]
                updateConfig({
                  llmProvider: provider,
                  llmBaseUrl: preset.baseUrl,
                  llmModel: preset.models[0] || '',
                })
              }}
              options={[
                { value: 'openai', label: t('providers.openai') },
                { value: 'deepseek', label: t('providers.deepseek') },
                { value: 'mimo', label: t('providers.mimo') },
                { value: 'custom', label: t('providers.custom') },
              ]}
            />
            {config.llmProvider !== 'custom' && PROVIDER_PRESETS[config.llmProvider].models.length > 0 && (
              <SelectField
                label={t('settings.model')}
                value={PROVIDER_PRESETS[config.llmProvider].models.includes(config.llmModel) ? config.llmModel : '__custom__'}
                onChange={(v) => {
                  if (v === '__custom__') {
                    updateConfig({ llmModel: '' })
                  } else {
                    updateConfig({ llmModel: v })
                  }
                }}
                options={[
                  ...PROVIDER_PRESETS[config.llmProvider].models.map((m) => ({ value: m, label: m })),
                  { value: '__custom__', label: t('settings.customModel') },
                ]}
              />
            )}
            {(config.llmProvider === 'custom' || !PROVIDER_PRESETS[config.llmProvider].models.includes(config.llmModel)) && (
              <Field
                label={t('settings.model')}
                value={config.llmModel}
                onChange={(v) => updateConfig({ llmModel: v })}
                placeholder="gpt-4o"
              />
            )}
            <Field
              label={t('settings.apiBaseUrl')}
              value={config.llmBaseUrl}
              onChange={(v) => updateConfig({ llmBaseUrl: v })}
              placeholder="https://api.openai.com/v1"
              readOnly={config.llmProvider !== 'custom'}
            />
            <Field
              label={t('settings.apiKey')}
              value={config.llmApiKey}
              onChange={(v) => updateConfig({ llmApiKey: v })}
              placeholder="sk-..."
              type="password"
            />
          </Section>

          {/* TTS Settings */}
          <Section title={t('settings.tts')}>
            <Field
              label={t('settings.apiKey')}
              value={config.ttsApiKey}
              onChange={(v) => updateConfig({ ttsApiKey: v })}
              placeholder="..."
              type="password"
            />
            <Field
              label={t('settings.voiceId')}
              value={config.ttsVoiceId}
              onChange={(v) => updateConfig({ ttsVoiceId: v })}
              placeholder="..."
            />
          </Section>

          {/* STT Settings */}
          <Section title={t('settings.stt')}>
            <Field
              label={t('settings.apiKey')}
              value={config.whisperApiKey}
              onChange={(v) => updateConfig({ whisperApiKey: v })}
              placeholder="..."
              type="password"
            />
            <Field
              label={t('settings.apiBaseUrl')}
              value={config.whisperBaseUrl}
              onChange={(v) => updateConfig({ whisperBaseUrl: v })}
              placeholder="..."
            />
            <Field
              label={t('settings.whisperModel')}
              value={config.whisperModel}
              onChange={(v) => updateConfig({ whisperModel: v })}
              placeholder="whisper-1"
            />
          </Section>

          {/* WebRTC */}
          <Section title={t('settings.webrtc')}>
            <Field
              label={t('settings.webrtcServer')}
              value={config.webrtcUrl}
              onChange={(v) => updateConfig({ webrtcUrl: v })}
              placeholder="http://localhost:8001"
            />
          </Section>

          {/* Language */}
          <Section title={t('settings.language')}>
            <SelectField
              label={t('settings.language')}
              value={i18n.language}
              onChange={(v) => i18n.changeLanguage(v)}
              options={[
                { value: 'zh', label: t('languages.zh') },
                { value: 'en', label: t('languages.en') },
                { value: 'ja', label: t('languages.ja') },
              ]}
            />
            <SelectField
              label={t('settings.voiceLang')}
              value={config.voiceOutputLanguage}
              onChange={(v) => updateConfig({ voiceOutputLanguage: v })}
              options={[
                { value: 'ja', label: t('languages.ja') },
                { value: 'zh', label: t('languages.zh') },
                { value: 'en', label: t('languages.en') },
              ]}
            />
            <SelectField
              label={t('settings.textLang')}
              value={config.textOutputLanguage}
              onChange={(v) => updateConfig({ textOutputLanguage: v })}
              options={[
                { value: 'zh', label: t('languages.zh') },
                { value: 'ja', label: t('languages.ja') },
                { value: 'en', label: t('languages.en') },
              ]}
            />
          </Section>

          {/* System Prompt */}
          <Section title={t('settings.personality')}>
            <textarea
              value={config.systemPrompt}
              onChange={(e) => updateConfig({ systemPrompt: e.target.value })}
              rows={4}
              className="w-full bg-black/30 border border-amadeus-border rounded px-3 py-2 text-xs text-gray-300 placeholder-gray-600 outline-none focus:border-primary/40 transition-colors resize-none"
            />
          </Section>
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-amadeus-border">
          <p className="text-[10px] text-gray-600 tracking-wider text-center">
            {t('settings.version')}
          </p>
        </div>
      </div>

      <style>{`
        @keyframes slide-in-left {
          from { transform: translateX(-100%); }
          to { transform: translateX(0); }
        }
        .animate-slide-in-left {
          animation: slide-in-left 0.3s ease-out;
        }
      `}</style>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="text-[10px] text-primary/60 tracking-widest uppercase mb-3">{title}</h4>
      <div className="space-y-3">{children}</div>
    </div>
  )
}

function Field({
  label, value, onChange, placeholder, type = 'text', readOnly = false,
}: {
  label: string; value: string; onChange: (v: string) => void; placeholder: string; type?: string; readOnly?: boolean
}) {
  return (
    <div>
      <label className="block text-[10px] text-gray-500 mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        readOnly={readOnly}
        className={`w-full bg-black/30 border border-amadeus-border rounded px-3 py-2 text-xs text-gray-300 placeholder-gray-600 outline-none focus:border-primary/40 transition-colors ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
      />
    </div>
  )
}

function SelectField({
  label, value, onChange, options,
}: {
  label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]
}) {
  return (
    <div>
      <label className="block text-[10px] text-gray-500 mb-1">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-black/30 border border-amadeus-border rounded px-3 py-2 text-xs text-gray-300 outline-none focus:border-primary/40 transition-colors"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </div>
  )
}
