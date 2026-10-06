import { buildOnboardingPrompt, mergeArtifacts, streamChat } from "../api";

function mockInvocation(text: string, events: unknown[], status = "completed") {
  const wire = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("");
  const bytes = new TextEncoder().encode(wire);
  const body = new ReadableStream({ start(controller) {
    // Split in the middle of an event to exercise network framing.
    controller.enqueue(bytes.slice(0, 19));
    controller.enqueue(bytes.slice(19));
    controller.close();
  } });
  const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true, body })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ status,
      output: { status, messages: [{ role: "assistant", content: text }] } }) });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
afterEach(() => vi.unstubAllGlobals());

test("streams native deltas and reads the persisted result with its session", async () => {
  const text = 'Hello world.\n```json apx-artifact\n{"type":"org_profile","current_systems":[{"category":"email","has_system":true}]}\n```';
  const fetchMock = mockInvocation(text, [
    { type: "agent.message.delta", message: { role: "assistant", content: "Hello " } },
    { type: "agent.message.delta", message: { role: "assistant", content: "world." } },
    { type: "run.completed" },
  ]);
  const streamed: string[] = [];
  const result = await streamChat({ message: "Discover", threadId: "session-1", onText: text => streamed.push(text) });
  const request = JSON.parse(fetchMock.mock.calls[0][1].body);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/invocations");
  expect(request).toMatchObject({ session_id: "session-1", input: { messages: [{ role: "user", content: "Discover" }] }, stream: true });
  expect(request.id).toMatch(/^[0-9a-f-]{36}$/);
  expect(fetchMock.mock.calls[1][0]).toBe(`/api/invocations/${request.id}`);
  expect(streamed).toEqual(["Hello ", "Hello world."]);
  expect(result.threadId).toBe("session-1");
  expect(result.reply).toBe("Hello world.");
  expect(result.artifacts[0].type).toBe("org_profile");
  expect(result.gate.filled).toEqual(["email"]);
  expect(result.gate.missing).toEqual(["docs", "financial", "crm", "fundraising"]);
});

test("rejects malformed artifacts from the persisted model output", async () => {
  mockInvocation('Done.\n```json apx-artifact\n{"type":"blueprint","lines":[{"domain":42}]}\n```', []);
  const result = await streamChat({ message: "finish", threadId: null });
  expect(result.artifacts).toEqual([]);
  expect(result.artifactError).toMatch(/artifact schema error/i);
});

test("accepts native buffered output without deltas", async () => {
  mockInvocation("Complete answer.", [{ type: "agent.message.completed", message: { role: "assistant", content: "Complete answer." } }]);
  const onText = vi.fn();
  const result = await streamChat({ message: "answer", threadId: null, onText });
  expect(result.reply).toBe("Complete answer.");
  expect(onText).toHaveBeenCalledWith("Complete answer.");
});

test("does not report a disconnected stream as a completed invocation", async () => {
  mockInvocation("Partial", [], "running");
  await expect(streamChat({ message: "answer", threadId: null })).rejects.toThrow("did not complete");
});

test("surfaces durable run failure", async () => {
  mockInvocation("", [{ type: "run.failed" }], "failed");
  await expect(streamChat({ message: "answer", threadId: null })).rejects.toThrow("invocation failed");
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
