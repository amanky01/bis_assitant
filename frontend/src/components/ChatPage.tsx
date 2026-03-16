"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  createSession,
  endSession,
  getSessionHistory,
  getUserHistory,
  streamMessage,
  type HistoryMessage,
  type SessionResponse,
  type UserSessionSummary,
} from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import ChatInput from "./ChatInput";
import ChatMessage, { type MessageMetadata } from "./ChatMessage";
import TypingIndicator from "./TypingIndicator";

type Message = {
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  metadata?: MessageMetadata | null;
};

const SUGGESTED_QUESTIONS = [
  "What is a hallmark?",
  "What is the ISI mark and how do I verify it?",
  "Verify CM/L-4521876 for me",
  "What IS standard applies to LPG regulators?",
  "How do I get BIS certification for my product?",
  "What is HUID in gold hallmarking?",
];

export default function ChatPage() {
  const { isLoggedIn, email, logout, isLoading: authLoading } = useAuth();
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [sessions, setSessions] = useState<UserSessionSummary[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [toolStatus, setToolStatus] = useState<string | null>(null);
  const [toolStatusTool, setToolStatusTool] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortStreamRef = useRef<(() => void) | null>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const refreshSessions = useCallback(async () => {
    if (!isLoggedIn) return;
    try {
      const list = await getUserHistory();
      setSessions(list);
    } catch {
      // ignore
    }
  }, [isLoggedIn]);

  const initSession = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await createSession();
      setSession(s);
      setMessages([]);
      if (isLoggedIn) {
        await refreshSessions();
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [isLoggedIn, refreshSessions]);

  useEffect(() => {
    initSession();
  }, [initSession]);

  const loadSession = useCallback(
    async (sessionId: string) => {
      if (!isLoggedIn) return;
      setLoading(true);
      setError(null);
      try {
        const history = await getSessionHistory(sessionId);
        setSession({
          session_id: sessionId,
          created_at: history[0]?.created_at ?? new Date().toISOString(),
          expires_at: null,
        });
        setMessages(
          history.map((m: HistoryMessage) => ({
            role: m.role as "user" | "assistant",
            content: m.content,
          }))
        );
        setSidebarOpen(false);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [isLoggedIn]
  );

  const handleNewChat = useCallback(() => {
    abortStreamRef.current?.();
    setSession(null);
    setMessages([]);
    initSession();
    setSidebarOpen(false);
  }, [initSession]);

  const handleDeleteChat = useCallback(
    async (e: React.MouseEvent, sessionId: string) => {
      e.stopPropagation();
      if (!isLoggedIn) return;
      try {
        await endSession(sessionId);
        await refreshSessions();
        if (session?.session_id === sessionId) {
          setSession(null);
          setMessages([]);
          initSession();
        }
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [isLoggedIn, session?.session_id, initSession, refreshSessions]
  );

  const handleSend = useCallback(
    async (text: string) => {
      if (!session || streaming) return;
      setError(null);
      setMessages((prev) => [...prev, { role: "user", content: text }]);
      setStreaming(true);
      let streamedContent = "";
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", isStreaming: true },
      ]);

      let lastMetadata: MessageMetadata | undefined;

      const abort = streamMessage(
        session.session_id,
        text,
        (chunk) => {
          if (chunk.type === "token") {
            streamedContent += chunk.content;
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  content: streamedContent,
                  isStreaming: true,
                };
              }
              return next;
            });
          } else if (chunk.type === "tool_status") {
            const status = chunk.tool_status?.status;
            const message = chunk.content || chunk.tool_status?.message;
            const tool = chunk.tool_status?.tool ?? null;
            setToolStatus(status === "running" && message ? message : null);
            setToolStatusTool(status === "running" ? tool : null);
          } else if (chunk.type === "metadata" && chunk.metadata) {
            lastMetadata = chunk.metadata as MessageMetadata;
          }
        },
        () => {
          setToolStatus(null);
          setToolStatusTool(null);
          setStreaming(false);
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                isStreaming: false,
                metadata: lastMetadata ?? last.metadata,
              };
            }
            return next;
          });
          abortStreamRef.current = null;
          refreshSessions();
        },
        (err) => {
          setToolStatus(null);
          setToolStatusTool(null);
          setError(err);
          setStreaming(false);
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && !last.content) {
              next[next.length - 1] = {
                role: "assistant",
                content: `Error: ${err}`,
                isStreaming: false,
              };
            }
            return next;
          });
          abortStreamRef.current = null;
        }
      );
      abortStreamRef.current = abort;
    },
    [session, streaming, refreshSessions]
  );

  if (authLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bis-blue-dark">
        <div className="flex flex-col items-center gap-3">
          <div className="w-10 h-10 border-2 border-bis-red border-t-transparent rounded-full animate-spin" />
          <p className="text-gray-400">Loading…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-bis-blue-dark overflow-hidden">
      {/* Sidebar: session history (logged-in only) */}
      {isLoggedIn && (
        <>
          <div
            className={`fixed inset-y-0 left-0 z-40 w-72 bg-bis-blue/95 backdrop-blur-md border-r border-bis-blue/60 transform transition-transform duration-200 ease-out ${
              sidebarOpen ? "translate-x-0" : "-translate-x-full"
            }`}
          >
            <div className="flex flex-col h-full">
              <div className="p-4 border-b border-bis-blue/60 flex items-center justify-between">
                <span className="font-semibold text-white">Chat history</span>
                <button
                  type="button"
                  onClick={() => setSidebarOpen(false)}
                  className="p-2 rounded-lg hover:bg-bis-blue-light/50 text-gray-400 hover:text-white transition-colors"
                  aria-label="Collapse sidebar"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                  </svg>
                </button>
              </div>
              <div className="flex-1 overflow-y-auto chat-scroll p-2">
                <button
                  type="button"
                  onClick={handleNewChat}
                  className="w-full py-2.5 px-3 rounded-xl bg-bis-red/80 hover:bg-bis-red text-white text-sm font-medium mb-2"
                >
                  + New chat
                </button>
                {sessions
                  .filter((s) => (s.message_count ?? 0) > 0)
                  .map((s) => (
                    <div
                      key={s.session_id}
                      className={`group flex items-center gap-1 rounded-xl mb-1 text-sm transition-colors ${
                        session?.session_id === s.session_id
                          ? "bg-bis-blue-light/80 text-white"
                          : "text-gray-400 hover:bg-bis-blue/60 hover:text-gray-200"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => loadSession(s.session_id)}
                        className="flex-1 min-w-0 text-left py-2 px-3 rounded-xl"
                      >
                        <span className="truncate block font-medium">
                          {s.title || new Date(s.created_at).toLocaleString()}
                        </span>
                        {s.message_count != null && s.message_count > 0 && (
                          <span className="text-xs opacity-80">
                            {s.message_count} messages
                          </span>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={(e) => handleDeleteChat(e, s.session_id)}
                        className="p-2 rounded-lg opacity-60 hover:opacity-100 hover:bg-bis-red/30 text-current shrink-0"
                        aria-label="Delete chat"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  ))}
                {sessions.filter((s) => (s.message_count ?? 0) > 0).length === 0 && !loading && (
                  <p className="text-gray-500 text-sm py-4">No past chats yet.</p>
                )}
              </div>
            </div>
          </div>
          {sidebarOpen && (
            <button
              type="button"
              onClick={() => setSidebarOpen(false)}
              className="fixed inset-0 z-30 bg-black/40 md:hidden"
              aria-label="Close overlay"
            />
          )}
        </>
      )}

      {/* Main chat area */}
      <div className="flex flex-col flex-1 min-w-0">
        {/* Header */}
        <header className="shrink-0 flex items-center justify-between px-4 py-3 bg-bis-blue/60 backdrop-blur-sm border-b border-bis-blue/60">
          <div className="flex items-center gap-3">
            {isLoggedIn && (
              <button
                type="button"
                onClick={() => setSidebarOpen(true)}
                className="p-2 rounded-lg hover:bg-bis-blue-light/50 text-gray-300"
                aria-label="Open history"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
            )}
            <div>
              <h1 className="text-lg font-bold text-white flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-bis-red" />
                BIS Assistant
              </h1>
              <p className="text-xs text-gray-400">
                Bureau of Indian Standards — Ask about standards & verification
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {isLoggedIn ? (
              <>
                <span className="text-sm text-gray-400 truncate max-w-[120px]">{email}</span>
                <button
                  type="button"
                  onClick={logout}
                  className="px-3 py-1.5 rounded-lg text-sm text-gray-400 hover:bg-bis-red/20 hover:text-bis-red-light transition-colors"
                >
                  Logout
                </button>
              </>
            ) : (
              <>
                <Link
                  href="/login"
                  className="px-3 py-1.5 rounded-lg text-sm text-gray-300 hover:bg-bis-blue-light/50"
                >
                  Sign in
                </Link>
                <Link
                  href="/register"
                  className="px-3 py-1.5 rounded-xl bg-bis-red hover:bg-bis-red-light text-white text-sm font-medium"
                >
                  Register
                </Link>
              </>
            )}
          </div>
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto chat-scroll p-4 space-y-4">
          {error && (
            <div className="px-4 py-2 rounded-xl bg-bis-red/20 border border-bis-red/40 text-bis-red-light text-sm">
              {error}
            </div>
          )}
          {loading && !session ? (
            <div className="flex justify-center py-12">
              <div className="w-8 h-8 border-2 border-bis-red border-t-transparent rounded-full animate-spin" />
            </div>
          ) : messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center px-4">
              <div className="w-16 h-16 rounded-2xl bg-bis-blue/80 border border-bis-red/30 flex items-center justify-center mb-4">
                <span className="text-3xl">◇</span>
              </div>
              <h2 className="text-xl font-semibold text-white mb-2">
                How can I help you today?
              </h2>
              <p className="text-gray-400 max-w-sm text-sm mb-6">
                Ask about BIS standards, hallmarking, ISI verification, or certification.
                {!isLoggedIn && " Sign in to save your chat history."}
              </p>
              <p className="text-[12px] font-medium text-gray-500 uppercase tracking-wider mb-3">
                Suggested questions
              </p>
              <div className="grid sm:grid-cols-2 gap-2 max-w-2xl">
                {SUGGESTED_QUESTIONS.map((q) => (
                  <button
                    key={q}
                    type="button"
                    onClick={() => !streaming && session && handleSend(q)}
                    disabled={streaming || !session}
                    className="text-left px-4 py-3 rounded-xl text-sm text-gray-300 hover:text-white bg-bis-blue/60 hover:bg-bis-blue/80 border border-bis-blue/60 hover:border-bis-red/40 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <ChatMessage
                key={`${i}-${m.content.slice(0, 20)}`}
                role={m.role}
                content={m.content}
                isStreaming={m.isStreaming}
                metadata={m.metadata}
              />
            ))
          )}
          {streaming && toolStatus && (
            <div className="flex flex-col gap-1 px-3 py-2 rounded-xl bg-bis-blue/40 border border-bis-blue/60 text-sm text-gray-300 w-full max-w-[85%] sm:max-w-[75%]">
              <div className="flex items-center gap-2">
                <div className="w-4 h-4 border-2 border-bis-red border-t-transparent rounded-full animate-spin shrink-0" />
                <span>{toolStatus}</span>
              </div>
              {["verify_cml", "verify_r_number", "verify_huid"].includes(toolStatusTool ?? "") && (
                <p className="text-xs text-gray-400 pl-6">This may take a few seconds for verification.</p>
              )}
            </div>
          )}
          {streaming && messages[messages.length - 1]?.role !== "assistant" && !toolStatus && (
            <TypingIndicator />
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="shrink-0">
          <ChatInput
            onSend={handleSend}
            disabled={!session || loading || streaming}
            placeholder="Ask about BIS standards, hallmarking, IS codes..."
          />
        </div>
      </div>
    </div>
  );
}
