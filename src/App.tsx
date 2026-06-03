import { Toaster } from 'react-hot-toast'
import { ChatProvider } from '@/store/chatStore'
import AppRoutes from './routes'

export default function App() {
  return (
    <ChatProvider>
      <div className="min-h-screen bg-amadeus-bg text-white">
        <AppRoutes />
        <Toaster position="top-right" />
      </div>
    </ChatProvider>
  )
}
