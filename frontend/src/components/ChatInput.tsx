"use client";

import { useRef, useState } from "react";

type ChatInputProps = {
  onSend: (message: string) => void;
  disabled?: boolean;
  placeholder?: string;
};

export default function ChatInput({
  onSend,
  disabled = false,
  placeholder = "Ask about BIS standards...",
}: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  return (
    <div className="flex gap-2 items-end p-3 bg-bis-blue/50 backdrop-blur-sm border-t border-bis-blue/60">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        className="flex-1 min-h-[44px] max-h-40 px-4 py-3 rounded-xl bg-bis-blue-dark/80 border border-bis-blue/60 text-gray-100 placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-bis-red/50 focus:border-bis-red/50 resize-none transition-all"
      />
      <button
        type="button"
        onClick={submit}
        disabled={disabled || !value.trim()}
        className="shrink-0 h-[44px] px-5 rounded-xl bg-bis-red hover:bg-bis-red-light disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-colors"
      >
        Send
      </button>
    </div>
  );
}
