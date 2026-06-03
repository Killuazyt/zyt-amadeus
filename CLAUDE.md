# ZYT-Amadeus 项目文档

> 命运石之门 Amadeus 系统 — AI 语音对话助手

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端框架 | React 18 + TypeScript + Vite |
| 样式 | Tailwind CSS 3 (暗色赛博朋克主题) |
| 状态管理 | React Context (`src/store/chatStore.tsx`) |
| 路由 | react-router-dom v6 |
| 桌面端 | Electron (暂未启用) |
| 后端 API | Hono + Node.js (`service/src/`) |
| 语音通信 | FastRTC + Python WebRTC (`service/webrtc/`) |
| 虚拟形象 | pixi-live2d-display + pixi.js 7 |
| UI 工具 | lucide-react 图标、clsx + tailwind-merge |

## 项目结构

```
zyt-amadeus/
├── CLAUDE.md                    # 本文档
├── package.json                 # 前端依赖 (npm)
├── vite.config.ts               # Vite 配置 (端口 1002, API 代理到 3002)
├── tailwind.config.js           # Tailwind 主题 (primary 青色, amadeus 暗色)
├── index.html                   # 入口 HTML
├── .env.example                 # 环境变量模板
│
├── public/                      # 静态资源
│   └── utils/live2d/            # Live2D SDK (pixi.js, cubism core, via script tag)
│
├── src/                         # 前端源码
│   ├── main.tsx                 # React 入口
│   ├── App.tsx                  # 根组件 (ChatProvider 包裹)
│   ├── pages/Home.tsx           # 首页，组合所有 UI 组件
│   ├── components/
│   │   ├── LoginOverlay.tsx     # 登录界面 (数字雨 + 赛博朋克风格)
│   │   ├── ParticleBackground.tsx  # 全屏粒子动画背景
│   │   ├── Live2dModel/           # Live2D 虚拟形象
│   │   │   ├── index.tsx      # Live2D 渲染组件 (pixi-live2d-display)
│   │   │   └── AnimationControl.ts  # 头部/眼部/眨眼动画控制
│   │   ├── DialogBox.tsx           # 底部最新 AI 回复显示
│   │   ├── ChatInput.tsx           # 底部输入框 (文本 + 语音按钮)
│   │   ├── ChatHistory.tsx         # 弹窗式完整对话历史
│   │   ├── ConfigPanel.tsx         # 设置侧边栏 (LLM/TTS/STT 配置)
│   │   └── Toolbar.tsx             # 右侧工具栏
│   ├── hooks/
│   │   ├── useWebRTC.ts        # WebRTC 通信 hook (音频流 + SSE 事件)
│   │   ├── useWebRTCChat.ts    # WebRTC ↔ ChatStore 桥接 hook
│   │   └── types.ts            # WebRTC 类型定义
│   ├── store/chatStore.tsx     # 对话状态管理 (Context + localStorage)
│   ├── types/chat.ts           # ChatMessage / Emotion 类型
│   ├── types/pixi-live2d.d.ts  # pixi-live2d-display 类型声明
│   ├── constants/live2d.ts     # Live2D 模型配置 (远程 CDN URL)
│   ├── constants/providers.ts  # API 提供商预设 (OpenAI/DeepSeek/MiMo)
│   ├── routes/index.tsx        # 路由配置
│   ├── lib/utils.ts            # cn() 工具函数
│   ├── i18n/                   # 多语言配置
│   │   ├── index.ts            # i18next 初始化
│   │   └── locales/            # 翻译文件 (zh/en/ja)
│   └── styles/index.css        # 全局样式 + Tailwind 指令
│
├── service/                     # 后端服务
│   ├── package.json             # 后端依赖 (Hono, tsup, tsx)
│   ├── src/index.ts             # Hono 服务入口 (端口 3002)
│   ├── src/chat.ts              # /api/chat 端点 (文本对话)
│   └── webrtc/                  # Python WebRTC 服务 (端口 8001)
│       ├── server.py            # FastAPI + FastRTC 主服务
│       ├── routes.py            # API 路由 (ICE config, events, config)
│       ├── ai.py                # LLM 流式对话 + 情绪预测
│       ├── stt.py               # Whisper 语音转文字
│       ├── tts.py               # 硅基流动 TTS
│       ├── utils.py             # 系统提示词生成 + 工具函数
│       ├── env.py               # 环境变量读取
│       └── requirements.txt     # Python 依赖
│
└── electron/                    # Electron 桌面端 (暂未启用)
    ├── main.mjs                 # 主进程
    └── preload.mjs              # 预加载脚本
```

## 已完成功能

### 前端 UI
- [x] 登录界面 (数字雨动画、网格覆盖、扫描线、赛博朋克风格)
- [x] 粒子动画背景 (青色粒子 + 连线效果)
- [x] Live2D 虚拟形象 (牧瀬紅莉栖，远程 CDN 加载，pixi.js 通过 script 标签加载，自动头部/眼部/眨眼动画)
- [x] Live2D 情绪驱动 (emotion → expressionManager)
- [x] Live2D 动作驱动 (motion → startMotion)
- [x] 底部对话框 (最新 AI 回复，加载动画)
- [x] 输入框 (Enter 发送，Shift+Enter 换行，语音按钮)
- [x] 对话历史弹窗 (完整消息列表，支持清空)
- [x] 设置侧边栏 (LLM/TTS/STT/WebRTC 配置)
- [x] 右侧工具栏 (对话记录/语音/设置)
- [x] 状态管理 (Context，localStorage 持久化)
- [x] 情绪系统类型 (normal/smile/blushing/angry/thinking/sad)
- [x] API 提供商预设 (OpenAI / DeepSeek / MiMo / 自定义，自动填充 URL 和模型)
- [x] 用户名持久化
- [x] 多语言 i18n (zh/en/ja，react-i18next，设置面板切换语言)

### WebRTC 通信 (useWebRTC hook)
- [x] WebRTCClient 类封装所有 WebRTC 逻辑
- [x] ICE config 从服务端获取
- [x] 音频流采集 + 发送
- [x] SSE 事件流 (transcript, llm_stream, emotion, next_action)
- [x] 静音检测 (2 秒阈值触发 onAudioSilence)
- [x] 麦克风开关
- [x] Live2D 口型同步 (预留接口)
- [x] 音频级别分析

### 后端 Node.js API
- [x] Hono 服务框架 (端口 3002)
- [x] CORS 跨域支持
- [x] /api/health 健康检查
- [x] /api/chat 文本对话端点 (OpenAI 兼容)

### 后端 Python WebRTC 服务
- [x] FastAPI + FastRTC 框架 (端口 8001)
- [x] WebRTC 音频流处理 (ReplyOnPause)
- [x] STT: Whisper 语音转文字
- [x] LLM: OpenAI 兼容流式对话
- [x] TTS: 硅基流动语音合成
- [x] 情绪预测 (独立 LLM 调用)
- [x] SSE 事件推送 (transcript, llm_stream, emotion)
- [x] 用户会话管理 (超时清理)
- [x] 热更新配置 (API key, 模型, 语言)
- [x] ICE/TURN 配置接口
- [x] Kurisu 人格系统提示词

### 未完成 / 待开发
- [ ] Electron 桌面端打包
- [ ] 视频帧分析 (摄像头)
- [ ] MEM0 长期记忆集成
- [ ] useWebRTCChat 语音对话集成（已暂时移除，会导致白屏）

### 已知问题
- Live2D 必须通过 `<script>` 标签加载 pixi.js（不能用 ESM import），文件在 `public/utils/live2d/`
- useWebRTCChat hook 暂时从 Home.tsx 移除，需要排查与 pixi-live2d-display 的冲突
- MiMo API 使用 `api-key` 请求头（非 `Authorization: Bearer`），后端已自动处理

## 启动方式

```bash
# 前端开发服务器
cd zyt-amadeus
npm install
npm run dev          # → http://localhost:1002

# 后端 Node.js API (需配置 .env)
cd service
npm install
npm run dev          # → http://localhost:3002

# 后端 Python WebRTC 服务 (需配置 .env)
cd service/webrtc
pip install -r requirements.txt
python server.py     # → http://localhost:8001
```

## 环境变量

复制 `.env.example` 为 `.env`，配置:
- `LLM_API_KEY` — OpenAI 或兼容 API 的密钥
- `LLM_BASE_URL` — API 基础 URL
- `LLM_MODEL` — 模型名称
- `WHISPER_API_KEY` — Whisper STT 密钥
- `TTS_API_KEY` — TTS 密钥
- `TTS_VOICE_ID` — TTS 语音 ID
- `WEBRTC_API_URL` — WebRTC 服务地址

## 参考项目

详见 `../Reference/REFERENCE.md`，主要参考:
- `amadeus-system-new` (309★) — 主要架构参考
- `Amadeus` (FrancescoCaracciolo) — Kurisu 人格 Prompt、对话数据
- `Amadeus-gpt` — 情绪系统设计参考
- `python-agents-amadeus` — LiveKit 实时语音 Agent 参考

## 设计规范

- **主色调**: 青色 `#00bcd4` (primary)
- **背景色**: 深蓝黑 `#0a0e17` (amadeus-bg)
- **卡片色**: 深灰蓝 `#111827` (amadeus-card)
- **边框色**: 暗蓝灰 `#1e293b` (amadeus-border)
- **字体**: Inter + Noto Sans SC
- **风格**: 赛博朋克、科幻感、暗色系、半透明毛玻璃
