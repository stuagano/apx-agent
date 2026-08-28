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
  const response = await fetch("/api/agents/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(threadId ? { message, threadId } : { message }),
  });
  if (!response.ok) throw new Error(`chat failed: ${response.status}`);
  if (!response.body) throw new Error("chat failed: streaming response body missing");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let reply = "";
  let nextThreadId = threadId;
  const streamedItems = new Set<string>();

  const consume = (block: string) => {
    const payload = block.split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!payload || payload === "[DONE]") return;
    const event = JSON.parse(payload) as Record<string, unknown>;
    if (event.type === "appkit.metadata" && record(event.data) && typeof event.data.threadId === "string") {
      nextThreadId = event.data.threadId;
    } else if (event.type === "response.output_text.delta" && typeof event.delta === "string") {
      if (typeof event.item_id === "string") streamedItems.add(event.item_id);
      reply += event.delta;
      onText?.(reply);
    } else if (event.type === "response.output_item.done" && record(event.item)
      && event.item.type === "message"
      && !(typeof event.item.id === "string" && streamedItems.has(event.item.id))
      && Array.isArray(event.item.content)) {
      const text = event.item.content.flatMap((part) =>
        record(part) && part.type === "output_text" && typeof part.text === "string" ? [part.text] : [],
      ).join("");
      reply += text;
      if (text) onText?.(reply);
    } else if (event.type === "error" || event.type === "response.failed") {
      throw new Error(typeof event.error === "string" ? event.error : "AppKit agent stream failed");
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    blocks.forEach(consume);
    if (done) break;
  }
  if (buffer.trim()) consume(buffer);

  const parsed = splitArtifacts(reply);
  return {
    reply: parsed.reply,
    threadId: nextThreadId,
    artifacts: parsed.artifacts,
    gate: gateFor(parsed.artifacts),
    artifactError: parsed.error,
  };
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

export type DevTool = { name: string; description: string; enabled: boolean; annotations: { effect: string } };
export type DevSkill = { name: string; description: string; content: string };
export type DevSnapshot = {
  agentName: string;
  model: string;
  originalModel: string;
  instructions: string;
  instructionsOverridden: boolean;
  tools: DevTool[];
  skills: DevSkill[];
  systemPrompt: string;
  overridesEphemeral: true;
};
export type AppKitThread = { id: string; messages: unknown[]; createdAt: string; updatedAt: string };

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`developer request failed: ${response.status}`);
  return response.json() as Promise<T>;
}

const body = (value: unknown): Pick<RequestInit, "headers" | "body"> => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

export const devGetConfig = () => json<DevSnapshot>("/api/dev/config");
export const devPatchConfig = (model: string) => json<DevSnapshot>("/api/dev/config", { method: "PATCH", ...body({ model }) });
export const devGetInstructions = () => json<DevSnapshot>("/api/dev/instructions");
export const devPatchInstructions = (instructions: string) => json<DevSnapshot>("/api/dev/instructions", { method: "PATCH", ...body({ instructions }) });
export const devResetInstructions = () => json<DevSnapshot>("/api/dev/instructions", { method: "DELETE" });
export const devGetToolset = () => json<DevSnapshot>("/api/dev/tools");
export const devToggleTool = (name: string, enabled: boolean) => json<DevSnapshot>(`/api/dev/tools/${encodeURIComponent(name)}`, { method: "PATCH", ...body({ enabled }) });
export const devAuthorSkill = (skill: DevSkill) => json<DevSnapshot>(`/api/dev/skills/${encodeURIComponent(skill.name)}`, { method: "PUT", ...body({ description: skill.description, content: skill.content }) });
export const devDeleteSkill = (name: string) => json<DevSnapshot>(`/api/dev/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
export const devGetFullPrompt = () => json<{ systemPrompt: string }>("/api/dev/prompt");
export const devListSessions = () => json<{ threads: AppKitThread[] }>("/api/agents/threads");
export const devDeleteSession = (id: string) => json<{ deleted: boolean }>(`/api/agents/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
