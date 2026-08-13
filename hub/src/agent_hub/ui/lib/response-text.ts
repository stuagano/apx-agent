type RecordValue = Record<string, unknown>;

function isRecord(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null;
}

function textFromContent(content: unknown): string[] {
  if (typeof content === "string") return [content];
  if (!Array.isArray(content)) return [];

  return content.flatMap((part) => {
    if (typeof part === "string") return [part];
    if (!isRecord(part) || typeof part.text !== "string") return [];
    return [part.text];
  });
}

function textFromItem(item: unknown): string[] {
  if (!isRecord(item)) return [];
  if (typeof item.text === "string") return [item.text];
  return textFromContent(item.content);
}

/** Extract human-facing assistant text from chat-completions or Responses payloads. */
export function extractResponseText(data: unknown): string | null {
  if (isRecord(data) && typeof data.output_text === "string" && data.output_text.trim()) {
    return data.output_text;
  }

  const items = Array.isArray(data)
    ? data
    : isRecord(data) && Array.isArray(data.output)
      ? data.output
      : [];

  const text = items
    .filter((item) => {
      if (!isRecord(item)) return false;
      if (item.type === "reasoning" || item.type === "function_call" || item.type === "tool_call") {
        return false;
      }
      return item.role === undefined || item.role === "assistant";
    })
    .flatMap(textFromItem)
    .join("");

  return text.trim() ? text : null;
}
