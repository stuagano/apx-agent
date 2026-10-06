export type Artifact = { type: string; [key: string]: unknown };
export type Gate = { complete: boolean; missing: string[]; filled: string[] };

export type ChatResult = {
  reply: string;
  threadId: string | null;
  artifacts: Artifact[];
  gate: Gate;
  artifactError: string | null;
};

const REQUIRED_CATEGORIES = ["email", "docs", "financial", "crm", "fundraising"];
const SYSTEM_CATEGORIES = new Set([
  ...REQUIRED_CATEGORIES,
  "grants", "program_case", "volunteer", "events", "comms", "back_office", "vertical",
]);
const BLUEPRINT_DECISIONS = new Set([
  "Keep&Integrate", "Migrate→Buy", "Migrate→Build", "New→Buy", "New→Build",
]);
const ARTIFACT_BLOCK = /```json\s+apx-artifact\s*\n([\s\S]*?)\n?```/gi;

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function optional(value: unknown, valid: (item: unknown) => boolean): boolean {
  return value === undefined || value === null || valid(value);
}

function validateArtifact(value: unknown): Artifact {
  if (!record(value) || typeof value.type !== "string") throw new Error("artifact must be an object with a type");

  if (value.type === "org_profile") {
    const strings = ["org_name", "budget_tier", "daily_vertical_workflow"];
    if (!strings.every((key) => optional(value[key], (item) => typeof item === "string"))) {
      throw new Error("org profile text fields must be strings");
    }
    if (!["staff_count", "volunteer_count"].every((key) => optional(value[key], (item) => typeof item === "number"))) {
      throw new Error("org profile counts must be numbers");
    }
    if (!optional(value.direct_service, (item) => typeof item === "boolean")) throw new Error("direct_service must be boolean");
    if (!optional(value.revenue_mix, record)) throw new Error("revenue_mix must be an object");
    if (!optional(value.compliance_surface, (item) => Array.isArray(item) && item.every((entry) => typeof entry === "string"))) {
      throw new Error("compliance_surface must be a string array");
    }
    if (!optional(value.current_systems, (item) => Array.isArray(item) && item.every((entry) =>
      record(entry)
      && typeof entry.category === "string"
      && SYSTEM_CATEGORIES.has(entry.category)
      && typeof entry.has_system === "boolean"
      && optional(entry.system_name, (field) => typeof field === "string")
      && optional(entry.keep_intent, (field) => ["keep", "open-to-change", "unsure"].includes(String(field))),
    ))) throw new Error("current_systems has an invalid entry");
    return value as Artifact;
  }

  if (value.type === "domain_relevance") {
    if (!Array.isArray(value.domains) || !value.domains.every((entry) =>
      record(entry)
      && typeof entry.domain === "string"
      && typeof entry.score === "number"
      && Number.isFinite(entry.score)
      && typeof entry.rationale === "string",
    )) throw new Error("domains has an invalid entry");
    return value as Artifact;
  }

  if (value.type === "blueprint") {
    if (!Array.isArray(value.lines) || !value.lines.every((entry) =>
      record(entry)
      && typeof entry.domain === "string"
      && optional(entry.current_system, (field) => typeof field === "string")
      && typeof entry.decision === "string"
      && BLUEPRINT_DECISIONS.has(entry.decision)
      && optional(entry.target, (field) => typeof field === "string")
      && typeof entry.justification === "string",
    )) throw new Error("lines has an invalid entry");
    return value as Artifact;
  }

  throw new Error(`unknown artifact type: ${value.type}`);
}

function splitArtifacts(text: string): { reply: string; artifacts: Artifact[]; error: string | null } {
  const artifacts: Artifact[] = [];
  const errors: string[] = [];
  const reply = text.replace(ARTIFACT_BLOCK, (_block, json: string) => {
    try {
      artifacts.push(validateArtifact(JSON.parse(json)));
    } catch (error) {
      errors.push(`artifact schema error: ${error instanceof Error ? error.message : String(error)}`);
    }
    return "";
  }).trim();
  return { reply, artifacts, error: errors.length ? errors.join("; ") : null };
}

function gateFor(artifacts: Artifact[]): Gate {
  const profile = [...artifacts].reverse().find((artifact) => artifact.type === "org_profile");
  const systems = Array.isArray(profile?.current_systems) ? profile.current_systems : [];
  const present = new Set(systems.flatMap((item) => record(item) && typeof item.category === "string" ? [item.category] : []));
  const filled = REQUIRED_CATEGORIES.filter((category) => present.has(category));
  const missing = REQUIRED_CATEGORIES.filter((category) => !present.has(category));
  return { complete: missing.length === 0, filled, missing };
}

export function mergeArtifacts(current: Artifact[], incoming: Artifact[]): { artifacts: Artifact[]; gate: Gate } {
  const byType = new Map(current.map((artifact) => [artifact.type, artifact]));
  incoming.forEach((artifact) => byType.set(artifact.type, artifact));
  const artifacts = [...byType.values()];
  return { artifacts, gate: gateFor(artifacts) };
}

type StreamChatOptions = {
  message: string;
  threadId: string | null;
  onText?: (text: string) => void;
};

export async function streamChat({ message, threadId, onText }: StreamChatOptions): Promise<ChatResult> {
  const id = crypto.randomUUID();
  const sessionId = threadId ?? crypto.randomUUID();
  const response = await fetch("/api/invocations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, session_id: sessionId, input: { messages: [{ role: "user", content: message }] }, stream: true }),
  });
  if (!response.ok) throw new Error(`chat failed: ${response.status}`);
  if (!response.body) throw new Error("chat failed: streaming response body missing");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let reply = "";
  const consume = (block: string) => {
    const payload = block.split(/\r?\n/).filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).trimStart()).join("\n");
    if (!payload) return;
    const event = JSON.parse(payload);
    if (event.type === "agent.message.delta" && event.message?.role === "assistant") {
      if (typeof event.message.content !== "string") throw new Error("Invalid agent message");
      reply += event.message.content;
      onText?.(reply);
    } else if (event.type === "agent.message.completed" && event.message?.role === "assistant") {
      if (typeof event.message.content !== "string") throw new Error("Invalid agent message");
      reply = event.message.content;
      onText?.(reply);
    } else if (event.type === "run.failed") {
      throw new Error("Agent invocation failed");
    }
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      blocks.forEach(consume);
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  // Read the persisted result; a disconnected stream is not proof of success.
  const result = await fetch(`/api/invocations/${id}`);
  if (!result.ok) throw new Error(`Invocation lookup failed: ${result.status}`);
  const invocation = await result.json();
  if (invocation.status !== "completed" || invocation.output?.status !== "completed") {
    throw new Error("Agent invocation did not complete");
  }
  const messages: unknown = invocation.output.messages;
  if (!Array.isArray(messages)) throw new Error("Invalid agent response");
  reply = messages.filter(item => record(item) && item.role === "assistant")
    .map(item => {
      if (typeof item.content !== "string") throw new Error("Invalid agent response");
      return item.content;
    }).join("\n");
  if (!reply) throw new Error("Agent returned no response");
  const parsed = splitArtifacts(reply);
  return { reply: parsed.reply, threadId: sessionId, artifacts: parsed.artifacts,
    gate: gateFor(parsed.artifacts), artifactError: parsed.error };
}

export async function buildOnboardingPrompt(url: string, files: File[]): Promise<string> {
  const parts: string[] = [];
  if (url.trim()) parts.push(`Organization website: ${url.trim()}`);
  for (const file of files) {
    const readable = file.type.startsWith("text/") || /\.(md|txt|csv|json)$/i.test(file.name);
    let content = `[Binary file: ${file.name}]`;
    if (readable) {
      try { content = await file.text(); } catch { /* keep the explicit binary note */ }
    }
    parts.push(`--- Document: ${file.name} ---\n${content}`);
  }
  return "The user has provided the following information about their organization:\n\n"
    + parts.join("\n\n")
    + "\n\nPlease analyze this information and begin the technology discovery process. "
    + "Ask targeted follow-up questions about their operations, pain points, and goals.";
}
