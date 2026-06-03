export type LlmProvider = 'openai' | 'deepseek' | 'mimo' | 'custom'

export interface ProviderPreset {
  name: string
  baseUrl: string
  models: string[]
}

export const PROVIDER_PRESETS: Record<LlmProvider, ProviderPreset> = {
  openai: {
    name: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1-mini'],
  },
  deepseek: {
    name: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    models: ['deepseek-chat', 'deepseek-reasoner'],
  },
  mimo: {
    name: 'MiMo (Xiaomi)',
    baseUrl: 'https://token-plan-cn.xiaomimimo.com/v1',
    models: ['mimo-v2.5-pro'],
  },
  custom: {
    name: 'Custom',
    baseUrl: '',
    models: [],
  },
}

export const PROVIDER_OPTIONS: { value: LlmProvider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'mimo', label: 'MiMo (Xiaomi)' },
  { value: 'custom', label: 'Custom' },
]
