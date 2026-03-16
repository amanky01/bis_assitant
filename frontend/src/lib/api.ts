const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const API_V1 = `${API_URL}/api/v1`;

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("bis_token");
}

function getAuthHeaders(): HeadersInit {
  const token = getToken();
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (token) (headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Auth ───────────────────────────────────────────────────────────────────

export type TokenResponse = {
  access_token: string;
  token_type: string;
  user_id: string;
  email: string;
};

export async function register(
  email: string,
  password: string
): Promise<TokenResponse> {
  const res = await fetch(`${API_V1}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error((d.detail as string) || "Registration failed");
  }
  return res.json();
}

export async function login(
  email: string,
  password: string
): Promise<TokenResponse> {
  const res = await fetch(`${API_V1}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error((d.detail as string) || "Login failed");
  }
  return res.json();
}

// ─── Chat sessions ───────────────────────────────────────────────────────────

export type SessionResponse = {
  session_id: string;
  created_at: string;
  expires_at: string | null;
};

export async function createSession(metadata?: Record<string, unknown>): Promise<SessionResponse> {
  const res = await fetch(`${API_V1}/chat/sessions`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ metadata: metadata ?? {} }),
  });
  if (!res.ok) throw new Error("Failed to create session");
  return res.json();
}

export async function getSession(sessionId: string): Promise<SessionResponse> {
  const res = await fetch(`${API_V1}/chat/sessions/${sessionId}`, {
    headers: getAuthHeaders(),
  });
  if (!res.ok) throw new Error("Session not found or expired");
  return res.json();
}

export async function endSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_V1}/chat/sessions/${sessionId}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  if (!res.ok) throw new Error("Failed to end session");
}

// ─── History (authenticated only) ────────────────────────────────────────────

export type HistoryMessage = {
  role: string;
  content: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
};

export async function getSessionHistory(
  sessionId: string
): Promise<HistoryMessage[]> {
  const res = await fetch(`${API_V1}/chat/sessions/${sessionId}/history`, {
    headers: getAuthHeaders(),
  });
  if (!res.ok) throw new Error("Failed to load history");
  return res.json();
}

export type UserSessionSummary = {
  session_id: string;
  created_at: string;
  message_count?: number;
  /** First user message in the chat (used as list title). */
  title?: string;
};

export async function getUserHistory(): Promise<UserSessionSummary[]> {
  const res = await fetch(`${API_V1}/chat/history`, {
    headers: getAuthHeaders(),
  });
  if (!res.ok) throw new Error("Authentication required");
  return res.json();
}

// ─── SSE stream chunk (matches backend StreamChunk) ───────────────────────────

export type StreamChunk =
  | { type: "token"; content: string }
  | { type: "metadata"; content: string; metadata?: Record<string, unknown> }
  | {
      type: "tool_status";
      content: string;
      tool_status?: { tool: string; status: "running" | "done"; message?: string };
    }
  | { type: "error"; content: string }
  | { type: "done"; content: string };

export function streamMessage(
  sessionId: string,
  message: string,
  onChunk: (chunk: StreamChunk) => void,
  onDone: () => void,
  onError: (err: string) => void
): () => void {
  const token = getToken();
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (token) (headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;

  const controller = new AbortController();
  fetch(`${API_V1}/chat/sessions/${sessionId}/message`, {
    method: "POST",
    headers,
    body: JSON.stringify({ session_id: sessionId, message }),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        onError((d.detail as string) || "Request failed");
        return;
      }
      const reader = res.body?.getReader();
      if (!reader) {
        onError("No response body");
        return;
      }
      const dec = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += dec.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const chunk = JSON.parse(line.slice(6)) as StreamChunk;
                onChunk(chunk);
                if (chunk.type === "error") onError(chunk.content);
                if (chunk.type === "done" || chunk.type === "error") onDone();
              } catch {
                // skip malformed
              }
            }
          }
        }
        if (buffer.startsWith("data: ")) {
          try {
            const chunk = JSON.parse(buffer.slice(6)) as StreamChunk;
            onChunk(chunk);
            if (chunk.type === "done" || chunk.type === "error") onDone();
          } catch {
            // skip
          }
        }
        onDone();
      } catch (e) {
        if ((e as Error).name !== "AbortError") onError((e as Error).message);
        onDone();
      }
    })
    .catch((e) => {
      onError((e as Error).message);
      onDone();
    });

  return () => controller.abort();
}

export function isLoggedIn(): boolean {
  return !!getToken();
}

export function setToken(token: string): void {
  if (typeof window !== "undefined") localStorage.setItem("bis_token", token);
}

export function clearToken(): void {
  if (typeof window !== "undefined") localStorage.removeItem("bis_token");
}
