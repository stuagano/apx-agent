import { useChat } from './useChat'
import { ChatPanel } from './ChatPanel'

export default function App() {
  const { messages, isLoading, sendMessage, reset } = useChat()
  return <ChatPanel messages={messages} isLoading={isLoading} onSend={sendMessage} onReset={reset} />
}
