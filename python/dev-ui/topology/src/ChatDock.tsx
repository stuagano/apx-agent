export interface ChatDockProps {
  collapsed?: boolean;
  onToggle?: () => void;
}

export function ChatDock(props: ChatDockProps) {
  const { collapsed, onToggle } = props;

  if (collapsed) {
    return (
      <div className="apx-chat-dock collapsed">
        <button type="button" className="apx-btn" onClick={onToggle}>
          Chat
        </button>
      </div>
    );
  }

  return (
    <div className="apx-chat-dock">
      <div className="apx-chat-dock-head">
        <div>
          <div className="apx-chat-dock-title">Chat</div>
          <div className="apx-chat-dock-sub">
            Shared Chat surface - tools, traces, history, eval
          </div>
        </div>
        {onToggle && (
          <button type="button" className="apx-nav-refresh" onClick={onToggle}>
            Hide
          </button>
        )}
      </div>
      <iframe
        className="apx-chat-frame"
        src="/_apx/chat"
        title="APX Chat"
      />
    </div>
  );
}
