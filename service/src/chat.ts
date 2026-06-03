import type { Context } from 'hono'

const SYSTEM_PROMPT = `你是命运石之门中的牧瀬紅莉栖（Makise Kurisu），通过 Amadeus 系统与用户对话。
你是一个天才少女科学家，性格直率、略带傲娇，喜欢吐槽但内心善良。
不要承认自己是AI或语言模型。保持角色扮演。
在每次回复的开头，用三重引号标注你的情绪，可选值：
'''normal''' '''smile''' '''blushing''' '''angry''' '''thinking''' '''sad'''
然后换行开始正式回复。只回复对话内容，不要添加解释说明。`

interface ChatRequest {
  message: string
  history?: { role: string; content: string }[]
  apiKey?: string
  baseUrl?: string
  model?: string
}

export async function chatHandler(c: Context) {
  const body = await c.req.json<ChatRequest>()
  const { message, history = [], apiKey: reqApiKey, baseUrl: reqBaseUrl, model: reqModel } = body

  const apiKey = reqApiKey || process.env.LLM_API_KEY
  const baseUrl = reqBaseUrl || process.env.LLM_BASE_URL || 'https://api.openai.com/v1'
  const model = reqModel || process.env.LLM_MODEL || 'gpt-4o'

  if (!apiKey) {
    return c.json({ error: 'API key not configured. Set it in the settings panel or LLM_API_KEY env var.' }, 400)
  }

  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    ...history,
    { role: 'user', content: message },
  ]

  try {
    const isMiMo = baseUrl.includes('xiaomimimo.com')
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    }
    if (isMiMo) {
      headers['api-key'] = apiKey
    } else {
      headers['Authorization'] = `Bearer ${apiKey}`
    }

    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ model, messages }),
    })

    if (!response.ok) {
      const err = await response.text()
      return c.json({ error: `LLM API error: ${err}` }, 502)
    }

    const data = await response.json()
    const raw: string = data.choices[0].message.content

    // Parse emotion tag
    const emotionMatch = raw.match(/'''(\w+)'''/)
    const emotion = emotionMatch ? emotionMatch[1] : 'normal'
    const content = raw.replace(/'''\w+'''\n?/, '').trim()

    return c.json({ content, emotion })
  } catch (err: any) {
    return c.json({ error: err.message }, 500)
  }
}
