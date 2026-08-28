import { useState, useRef } from "react";

type Props = {
  hasMessages: boolean;
  onSubmit: (url: string, files: File[]) => void;
  busy: boolean;
};

export function OnboardingPanel({ hasMessages, onSubmit, busy }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [url, setUrl] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const isMinimized = collapsed || (submitted && hasMessages);

  function handleSubmit() {
    if (!url.trim() && files.length === 0) return;
    setSubmitted(true);
    onSubmit(url, files);
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files) {
      setFiles(Array.from(e.target.files));
    }
  }

  if (isMinimized) {
    return (
      <div className="onboarding-panel onboarding-panel--minimized">
        <button
          className="onboarding-toggle"
          onClick={() => setCollapsed(false)}
        >
          <span className="onboarding-toggle__icon">i</span>
          <span>Getting Started</span>
          <span className="onboarding-toggle__expand">Expand</span>
        </button>
        {submitted && (
          <div className="onboarding-help-links">
            <a href="#" className="onboarding-help-link">How it works</a>
            <a href="#" className="onboarding-help-link">FAQ</a>
            <a href="#" className="onboarding-help-link">Contact support</a>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="onboarding-panel">
      {hasMessages && (
        <button
          className="onboarding-collapse-btn"
          onClick={() => setCollapsed(true)}
          aria-label="Minimize"
        >
          &minus;
        </button>
      )}
      <div className="onboarding-content">
        <p className="onboarding-intro">
          This is a tool for discovering how technology might be able to improve the way
          you deliver on your mission. Please start by providing the website URL for your
          organization and uploading any operating documents, SOPs, or other information
          describing how your nonprofit operates. Our consultant will extract as much
          information as possible from this information, and follow up with targeted
          questions aimed at discovering in what ways technology can help you run more
          efficiently.
        </p>

        <div className="onboarding-form">
          <div className="onboarding-field">
            <label className="onboarding-label" htmlFor="org-url">
              Organization Website URL
            </label>
            <input
              id="org-url"
              className="onboarding-input"
              type="url"
              placeholder="https://yourorganization.org"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={busy}
            />
          </div>

          <div className="onboarding-field">
            <label className="onboarding-label">
              Operating Documents
            </label>
            <div className="onboarding-upload-row">
              <button
                className="onboarding-upload-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy}
                type="button"
              >
                Choose Files
              </button>
              <span className="onboarding-file-count">
                {files.length === 0
                  ? "No files selected"
                  : `${files.length} file${files.length > 1 ? "s" : ""} selected`}
              </span>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".pdf,.doc,.docx,.txt,.md"
                onChange={handleFileChange}
                style={{ display: "none" }}
              />
            </div>
          </div>

          <button
            className="onboarding-submit-btn"
            onClick={handleSubmit}
            disabled={busy || (!url.trim() && files.length === 0)}
          >
            {busy ? "Processing..." : "Start Discovery"}
          </button>
        </div>
      </div>
    </div>
  );
}
