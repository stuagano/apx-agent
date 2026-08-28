import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DevToolbar } from "../components/DevToolbar";

afterEach(() => vi.unstubAllGlobals());

test("uses the shared five-tab runtime and real AppKit threads", async () => {
  const snapshot = {
    agentName: "discovery",
    model: "databricks-claude-sonnet-4-6",
    originalModel: "databricks-claude-sonnet-4-6",
    instructions: "Discover the nonprofit's needs.",
    instructionsOverridden: false,
    tools: [],
    skills: [],
    systemPrompt: "Discover the nonprofit's needs.",
    overridesEphemeral: true,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/agents/threads") {
      return {
        ok: true,
        json: async () => ({
          threads: [{ id: "thread-123", messages: [{ role: "user" }], createdAt: "now", updatedAt: "now" }],
        }),
      } as Response;
    }
    if (url === "/api/agents/threads/thread-123" && init?.method === "DELETE") {
      return { ok: true, json: async () => ({ deleted: true }) } as Response;
    }
    return { ok: true, json: async () => snapshot } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  const onReset = vi.fn();

  render(<DevToolbar threadId="thread-123" onReset={onReset} />);
  fireEvent.click(screen.getByRole("button", { name: "Dev" }));

  for (const name of ["Config", "Instructions", "Tools", "Sessions", "Prompt"]) {
    expect(screen.getByRole("tab", { name })).toBeTruthy();
  }

  fireEvent.click(screen.getByRole("tab", { name: "Sessions" }));
  expect(await screen.findByText((_, element) => element?.textContent?.includes("1 message") ?? false, { selector: "span" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Delete thread thread-123" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/agents/threads/thread-123",
    { method: "DELETE" },
  ));
});
