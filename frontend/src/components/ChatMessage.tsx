"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type ToolCallInfo = {
  tool_name: string;
  input_summary: string;
  outcome: string;
  result_preview?: string;
};

export type MessageMetadata = {
  tool_calls?: ToolCallInfo[];
  sources?: string[];
  processing_time_ms?: number;
};

type ChatMessageProps = {
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  metadata?: MessageMetadata | null;
};

export default function ChatMessage({
  role,
  content,
  isStreaming = false,
  metadata,
}: ChatMessageProps) {
  const isUser = role === "user";

  return (
    <div
      className={`flex w-full animate-fade-in-up ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[85%] sm:max-w-[75%] px-4 py-2.5 rounded-2xl shadow-lg ${
          isUser ? "message-user rounded-br-md" : "message-assistant rounded-bl-md"
        }`}
      >
        {isUser ? (
          <p className="text-sm sm:text-base whitespace-pre-wrap break-words">
            {content || "—"}
          </p>
        ) : (
          <>
            <div className="text-sm sm:text-base break-words prose prose-invert max-w-none prose-p:my-2 prose-p:first:mt-0 prose-p:last:mb-0 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-strong:text-white prose-a:text-bis-red-light prose-a:underline prose-a:no-underline hover:prose-a:underline">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  a: ({ href, children }) => (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-bis-red-light hover:underline"
                    >
                      {children}
                    </a>
                  ),
                }}
              >
                {content || (isStreaming ? "" : "—")}
              </ReactMarkdown>
            </div>
            {isStreaming && (
              <span className="inline-block w-2 h-4 ml-0.5 bg-bis-red animate-cursor-blink" />
            )}
          </>
        )}
        {/* Sources and processing time (tools used hidden — not useful for end user) */}
        {!isUser && metadata && !isStreaming && (
          <div className="mt-3 pt-3 border-t border-white/10 space-y-2">
            {metadata.sources && metadata.sources.length > 0 && (() => {
              const normalized = metadata.sources.map((raw): { href: string; label: string } => {
                const href = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
                try {
                  const u = new URL(href);
                  const path = u.pathname || "/";
                  const fullUrl = u.origin + path + (u.search || "");
                  const label = path === "/" || path === "" ? u.hostname : fullUrl;
                  return { href, label };
                } catch {
                  return { href, label: raw.replace(/^https?:\/\//i, "").slice(0, 80) };
                }
              });
              const seen = new Set<string>();
              const unique = normalized.filter(({ href }) => {
                if (seen.has(href)) return false;
                seen.add(href);
                return true;
              });
              return (
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs text-gray-400 font-medium">Sources:</span>
                  {unique.map(({ href, label }) => (
                    <a
                      key={href}
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-bis-red-light hover:underline break-all"
                      title={href}
                    >
                      {label.length > 100 ? `${label.slice(0, 97)}…` : label}
                    </a>
                  ))}
                </div>
              );
            })()}
            {metadata.processing_time_ms != null && (
              <p className="text-[11px] text-gray-500 font-mono">
                {metadata.processing_time_ms}ms
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
