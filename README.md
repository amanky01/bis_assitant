# BIS Assistant — Intelligent Standards Chatbot

A production-grade AI assistant for BIS (Bureau of Indian Standards) queries.
Built with FastAPI + LangGraph + LangChain + MongoDB Atlas + Next.js 14.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Next.js Frontend                         │
│  Chat Interface → SSE streaming → Conversation state (Zustand)  │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP / SSE
┌─────────────────────────▼───────────────────────────────────────┐
│                      FastAPI Backend                             │
│                                                                  │
│  POST /api/v1/chat/message  (streaming SSE)                      │
│  POST /api/v1/chat/session  (create session)                     │
│  DELETE /api/v1/chat/session/{id}  (end session)                 │
│  GET  /api/v1/health                                             │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  LangGraph Agent                          │   │
│  │                                                          │   │
│  │  START → [classify_intent] → [retrieve_context]          │   │
│  │       → [check_verification] → [generate_response]       │   │
│  │       → [format_output] → END                            │   │
│  │                                                          │   │
│  │  State: AgentState (messages, intent, context, session)  │   │
│  └────────────────────────┬─────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────▼─────────────────────────────────┐   │
│  │                    Tools Layer                            │   │
│  │  • VectorSearchTool  — MongoDB Atlas Vector Search        │   │
│  │  • BISScraperTool    — manakonline.in / crsbis.in         │   │
│  │  • HUIDVerifierTool  — huid.manakonline.in               │   │
│  │  • CategoryMatchTool — IS number ↔ product category      │   │
│  └────────────────────────┬─────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────▼─────────────────────────────────┐   │
│  │                  MongoDB Atlas                            │   │
│  │  • bis_knowledge (vector store — embeddings)             │   │
│  │  • sessions      (conversation memory — TTL indexed)     │   │
│  │  • is_standards  (IS number → product category map)      │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Session Memory Design

- Each chat session gets a unique `session_id` (UUID)
- Messages are stored in MongoDB with TTL (default: 2 hours)
- Sliding window: last N messages sent as context (configurable, default: 10)
- Session ends when user closes tab or explicitly ends it
- No cross-session memory — each conversation is stateless across sessions

## Quick Start

### Backend
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
uvicorn app.main:app --reload --port 8000
```

### Vector DB Seeding
```bash
cd backend
python scripts/seed_vector_db.py --source data/bis_knowledge.json
python scripts/seed_vector_db.py --source data/is_standards_map.json --collection is_standards
```

### Frontend
```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

## Environment Variables

### Backend (.env)
```
MONGODB_URI=mongodb+srv://...
MONGODB_DB_NAME=bis_assistant
OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY
LLM_PROVIDER=openai            # openai | anthropic
LLM_MODEL=gpt-4o               # gpt-4o | claude-sonnet-4-6
EMBEDDING_MODEL=text-embedding-3-small
SESSION_TTL_HOURS=2
SESSION_WINDOW_SIZE=10
CORS_ORIGINS=http://localhost:3000
```

### Frontend (.env.local)
```
NEXT_PUBLIC_API_URL=http://localhost:8000
```
