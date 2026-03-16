# BIS Assistant Frontend

Modern chat UI for the BIS Assistant API (Next.js, TypeScript, Tailwind CSS).

## Features

- **Chat with or without login** — Use the assistant anonymously or sign in for persistent history.
- **Persistent chat memory** — Logged-in users get saved sessions and can resume past chats.
- **Chat list** — Sidebar shows only chats that have at least one message; each chat is titled by the first question you asked.
- **Sidebar** — Collapse with the arrow (←) in the sidebar header; reopen with the menu icon in the main header.
- **SSE streaming** — Responses stream in real time with a typing-style animation.
- **BIS theme** — Blue and red color palette aligned with Bureau of Indian Standards branding.

### Chat context and navigation

- **One chat = one session.** The backend stores messages per `session_id`. Context (past messages sent to the LLM) is only from the **current** chat.
- **Switching chats:** Click a chat in the sidebar → that session’s messages load; your next message is sent in that session. No context is shared between different chats.
- **New chat:** “+ New chat” creates a new session. It appears in the sidebar only after you send the first message (then it shows the first question as the title).
- **Closing the app** does not “close” a chat; when you sign in again, your sessions and history are still there. Each chat’s context stays isolated by `session_id`.

## Setup

1. Copy env and set API URL:
   ```bash
   cp .env.local.example .env.local
   ```
   Edit `.env.local` and set `NEXT_PUBLIC_API_URL` to your backend (e.g. `http://localhost:8000`).

2. Install and run:
   ```bash
   npm install
   npm run dev
   ```
   Open [http://localhost:3000](http://localhost:3000).

## Backend

Ensure the FastAPI backend is running and CORS allows `http://localhost:3000`. See backend `.env` for `CORS_ORIGINS`.
