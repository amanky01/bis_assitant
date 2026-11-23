const API_V1 = "/api/v1";
const guestTokens = new Map<string, string>();

function getAuthHeaders(sessionId?: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-BIS-Client": "web",
  };
  const guestToken = sessionId ? guestTokens.get(sessionId) : undefined;
  if (guestToken) headers["X-Guest-Token"] = guestToken;
  return headers;
}

// ─── Auth ───────────────────────────────────────────────────────────────────

export type AuthUser = {
  user_id: string;
  email: string;
};

export async function register(
  email: string,
  password: string
): Promise<AuthUser> {
  const res = await fetch(`${API_V1}/auth/register`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(typeof d.detail === "string" ? d.detail : d.detail?.[0]?.msg || "Registration failed");
  }
  return res.json();
}

export async function login(
  email: string,
  password: string
): Promise<AuthUser> {
  const res = await fetch(`${API_V1}/auth/login`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(typeof d.detail === "string" ? d.detail : "Login failed");
  }
  return res.json();
}

// ─── Chat sessions ───────────────────────────────────────────────────────────

export type SessionResponse = {
  session_id: string;
  created_at: string;
  expires_at: string | null;
  guest_token?: string | null;
};

export async function createSession(metadata?: Record<string, unknown>): Promise<SessionResponse> {
  const res = await fetch(`${API_V1}/chat/sessions`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify({ metadata: metadata ?? {} }),
  });
  if (!res.ok) throw new Error("Failed to create session");
  const session: SessionResponse = await res.json();
  if (session.guest_token) guestTokens.set(session.session_id, session.guest_token);
  return session;
}

export async function getSession(sessionId: string): Promise<SessionResponse> {
  const res = await fetch(`${API_V1}/chat/sessions/${sessionId}`, {
    headers: getAuthHeaders(sessionId),
  });
  if (!res.ok) throw new Error("Session not found or expired");
  return res.json();
}

export async function endSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_V1}/chat/sessions/${sessionId}`, {
    method: "DELETE",
    headers: getAuthHeaders(sessionId),
  });
  if (!res.ok) throw new Error("Failed to end session");
  guestTokens.delete(sessionId);
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
    headers: getAuthHeaders(sessionId),
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
  const controller = new AbortController();

  async function readStream() {
    try {
      const res = await fetch(`${API_V1}/chat/sessions/${sessionId}/message`, {
        method: "POST",
        headers: { ...getAuthHeaders(sessionId), "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ session_id: sessionId, message }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
      }
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (true) {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          if (done && buffer) frames.push(buffer);
          for (const frame of frames) {
            if (!frame.startsWith("data: ")) continue;
            let chunk: StreamChunk;
            try {
              chunk = JSON.parse(frame.slice(6)) as StreamChunk;
            } catch {
              continue;
            }
            onChunk(chunk);
            if (chunk.type === "error") throw new Error(chunk.content);
            if (chunk.type === "done") return;
          }
          if (done) throw new Error("Connection closed before the response finished.");
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
    } catch (error) {
      if (!controller.signal.aborted) onError((error as Error).message);
    } finally {
      if (!controller.signal.aborted) onDone();
    }
  }

  void readStream();
  return () => controller.abort();
}

export async function getCurrentUser(): Promise<AuthUser | null> {
  const res = await fetch(`${API_V1}/auth/me`, { cache: "no-store" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error("Could not check your session");
  return res.json();
}

export async function logout(): Promise<void> {
  const res = await fetch(`${API_V1}/auth/logout`, {
    method: "POST",
    headers: getAuthHeaders(),
  });
  if (!res.ok) throw new Error("Could not sign out. Try again.");
  guestTokens.clear();
}
