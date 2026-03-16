"use client";

export default function TypingIndicator() {
  return (
    <div className="flex items-center gap-1.5 px-4 py-2 rounded-2xl message-assistant w-fit animate-fade-in-up">
      <span
        className="w-2 h-2 rounded-full bg-bis-red animate-typing-dot"
        style={{ animationDelay: "0s" }}
      />
      <span
        className="w-2 h-2 rounded-full bg-bis-red animate-typing-dot"
        style={{ animationDelay: "0.2s" }}
      />
      <span
        className="w-2 h-2 rounded-full bg-bis-red animate-typing-dot"
        style={{ animationDelay: "0.4s" }}
      />
    </div>
  );
}
