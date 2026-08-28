import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "../App";
import { BlueprintView } from "../components/BlueprintView";
import type { Artifact } from "../api";

function chatResponse(): Response {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(
        'data: {"type":"appkit.metadata","data":{"threadId":"thread-123"}}\n\n'
        + 'data: {"type":"response.output_text.delta","delta":"What email tool do you use?"}\n\n'
        + 'data: {"type":"response.completed","response":{}}\n\n',
      ));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as Response;
}

beforeEach(() => {
  globalThis.fetch = (async (input: RequestInfo | URL) => String(input) === "/api/dev-ui"
    ? { ok: true, json: async () => ({ enabled: true }) } as Response
    : chatResponse()) as typeof fetch;
});

test("chat is hidden until onboarding is submitted", () => {
  render(<App />);
  expect(screen.getByText(/chat will become available/i)).toBeTruthy();
  expect(screen.queryByPlaceholderText("Tell us about your organization...")).toBeNull();
});

test("sends a message and renders the assistant reply", async () => {
  render(<App />);
  fireEvent.change(screen.getByPlaceholderText("https://yourorganization.org"), { target: { value: "https://example.org" } });
  fireEvent.click(screen.getByText("Start Discovery"));
  await waitFor(() => expect(screen.getByPlaceholderText("Tell us about your organization...")).toBeTruthy());
  fireEvent.change(screen.getByPlaceholderText("Tell us about your organization..."), { target: { value: "hi" } });
  fireEvent.click(screen.getByText(/send/i));
  await waitFor(() => expect(screen.getByText(/what email tool/i)).toBeTruthy());
});

test("blueprint view renders decision lines", () => {
  const bp: Artifact = { type: "blueprint", lines: [
    { domain: "financial", current_system: "QuickBooks", decision: "Keep&Integrate",
      target: null, justification: "Regulatory trust." },
    { domain: "volunteer", current_system: null, decision: "New→Build",
      target: "Volunteer Management", justification: "Vertical gap." },
  ] };
  render(<BlueprintView artifact={bp} />);
  expect(screen.getByText(/QuickBooks/)).toBeTruthy();
  expect(screen.getByText(/Volunteer Management/)).toBeTruthy();
  expect(screen.getByText(/Keep&Integrate/)).toBeTruthy();
});

test("hides the default-on developer launcher only when explicitly disabled", async () => {
  globalThis.fetch = (async (input: RequestInfo | URL) => ({
    ok: true,
    json: async () => String(input) === "/api/dev-ui" ? { enabled: false } : {},
  })) as typeof fetch;

  render(<App />);

  await waitFor(() => expect(screen.queryByRole("button", { name: "Dev" })).toBeNull());
});
