export class ApiError extends Error {
  constructor(message, status, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

const parseResponse = async (response) => {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
};

export const requestJson = async (url, options = {}) => {
  const headers = { Accept: "application/json", ...options.headers };

  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(url, { ...options, headers });

  const payload = await parseResponse(response);

  if (!response.ok) {
    const message =
      payload?.detail || payload?.error || `Request failed (${response.status})`;
    throw new ApiError(message, response.status, payload);
  }

  return payload;
};

export const streamSse = async function* (url, options = {}) {
  const headers = { Accept: "text/event-stream", ...options.headers };

  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(url, { ...options, headers });

  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(`SSE stream failed (${response.status})`, response.status, body);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop();

    for (const part of parts) {
      const trimmed = part.trim();
      if (!trimmed.startsWith("data: ")) continue;

      try {
        const frame = JSON.parse(trimmed.slice(6));
        const event = frame.event;
        const data = frame.data;
        if (data?.text) data.text = data.text.replace(/\u2014/g, "--");
        yield { event, data };
      } catch {
        // skip unparseable frames
      }
    }
  }
};