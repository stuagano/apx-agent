import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "../App";
import { BlueprintView } from "../components/BlueprintView";
import type { Artifact } from "../api";

beforeEach(() => {
  globalThis.fetch = vi.fn(async (_input, init) => init?.method === "POST"
    ? { ok: true, body: new ReadableStream({ start(controller) { controller.close(); } }) }
    : { ok: true, json: async () => ({ status: "completed", output: { status: "completed", messages: [{ role: "assistant", content: "What email tool do you use?" }] } }) }) as typeof fetch;
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

test("new conversation resets onboarding without deleting managed history", async () => {
  render(<App />);
  fireEvent.change(screen.getByPlaceholderText("https://yourorganization.org"), { target: { value: "https://example.org" } });
  fireEvent.click(screen.getByText("Start Discovery"));
  await screen.findByText(/what email tool/i);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  expect(screen.getByText(/chat will become available/i)).toBeTruthy();
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
});
