import { buildOnboardingPrompt, mergeArtifacts, streamChat } from "../api";

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as Response;
}

afterEach(() => vi.unstubAllGlobals());

test("streams native AppKit chat and returns its thread and validated artifact", async () => {
  const fetchMock = vi.fn().mockResolvedValue(sseResponse([
    'data: {"type":"appkit.metadata","data":{"threadId":"thread-123"}}\n\n',
    'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n',
    'data: {"type":"response.output_text.delta","delta":"world.\\n```json apx-artifact\\n{\\"type\\":\\"org_profile\\",\\"current_systems\\":[{\\"category\\":\\"email\\",\\"has_system\\":true}]}\\n```"}\n\n',
    'data: {"type":"response.completed","response":{}}\n\n',
  ]));
  vi.stubGlobal("fetch", fetchMock);
  const streamed: string[] = [];

  const result = await streamChat({
    message: "Tell me what you found",
    threadId: null,
    onText: (text) => streamed.push(text),
  });

  expect(fetchMock).toHaveBeenCalledWith("/api/agents/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "Tell me what you found" }),
  });
  expect(streamed[0]).toBe("Hello ");
  expect(streamed.at(-1)).toContain("org_profile");
  expect(result.threadId).toBe("thread-123");
  expect(result.reply).toBe("Hello world.");
  expect(result.artifacts).toEqual([
    { type: "org_profile", current_systems: [{ category: "email", has_system: true }] },
  ]);
  expect(result.gate).toEqual({
    complete: false,
    filled: ["email"],
    missing: ["docs", "financial", "crm", "fundraising"],
  });
});

test("rejects malformed artifacts at the streamed model-output boundary", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
    'data: {"type":"response.output_text.delta","delta":"Done.\\n```json apx-artifact\\n{\\"type\\":\\"blueprint\\",\\"lines\\":[{\\"domain\\":42}]}\\n```"}\n\n',
    'data: {"type":"response.completed","response":{}}\n\n',
  ])));

  const result = await streamChat({ message: "finish", threadId: "thread-123" });

  expect(result.artifacts).toEqual([]);
  expect(result.artifactError).toMatch(/artifact schema error/i);
});

test("accepts AppKit's completed message item when an adapter emits no deltas", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
    'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"Complete answer."}]}}\n\n',
    'data: {"type":"response.completed","response":{}}\n\n',
  ])));

  const result = await streamChat({ message: "answer", threadId: null });

  expect(result.reply).toBe("Complete answer.");
});

test("builds onboarding context locally without a second backend", async () => {
  const textFile = {
    name: "operations.txt",
    type: "text/plain",
    text: async () => "We coordinate volunteers by email.",
  } as File;
  const binaryFile = {
    name: "handbook.pdf",
    type: "application/pdf",
    text: async () => { throw new Error("binary"); },
  } as File;

  const prompt = await buildOnboardingPrompt("https://example.org", [textFile, binaryFile]);

  expect(prompt).toContain("Organization website: https://example.org");
  expect(prompt).toContain("We coordinate volunteers by email.");
  expect(prompt).toContain("[Binary file: handbook.pdf]");
  expect(prompt).toContain("begin the technology discovery process");
});

test("merges streamed artifacts by type and keeps the organization gate", () => {
  const current = [{
    type: "org_profile",
    current_systems: [{ category: "email", has_system: true }],
  }];
  const next = [{
    type: "domain_relevance",
    domains: [{ domain: "fundraising", score: 0.9, rationale: "Manual work" }],
  }];

  const merged = mergeArtifacts(current, next);

  expect(merged.artifacts.map((artifact) => artifact.type)).toEqual(["org_profile", "domain_relevance"]);
  expect(merged.gate.filled).toEqual(["email"]);
});
