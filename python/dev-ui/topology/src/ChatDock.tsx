import { useEffect } from "react";

export interface ChatDockProps {
  collapsed?: boolean;
  onToggle?: () => void;
  onSendingChange?: (sending: boolean) => void;
}

export function ChatDock(props: ChatDockProps) {
  const { collapsed, onToggle, onSendingChange } = props;

  useEffect(() => {
    onSendingChange?.(false);
  }, [onSendingChange]);

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
      <iframe
        className="apx-chat-frame"
        src="/_apx/chat?embed=1"
        title="APX Chat"
      />
    </div>
  );
}
