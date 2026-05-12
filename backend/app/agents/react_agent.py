"""
app/agents/react_agent.py
──────────────────────────
LangGraph ReAct agent for the BIS Assistant.

Design:
  - Standard ReAct loop: Think → Act (tool call) → Observe → repeat
  - Hard cap: AGENT_MAX_ITERATIONS (env-configurable)
  - On cap hit: generate partial answer + explicitly list what was not verified
  - All 9 tools registered; agent picks based on system prompt guidance
  - Streams tokens back to the API layer via AsyncGenerator

Tool priority the agent is instructed to follow:
  1. search_bis_knowledge (vector DB — fastest, most reliable)
  2. verify_* tools (direct portal scrape — when identifier present)
  3. web_search_bis → scrape_page (fallback — when vector DB misses)
  4. get_compliance_guide (static — for certification process questions)
  5. check_category_match / detect_fake_mark (after verify_cml succeeds)
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.prebuilt import create_react_agent

from app.core.config import get_settings
from app.core.exceptions import AgentError
from app.core.logging import get_logger
from app.schemas.chat import ResponseMetadata, ToolCallRecord, UnverifiedItem
from app.services.gemini import get_llm
from app.tools.compliance import get_compliance_guide
from app.tools.vector_search import search_bis_knowledge
from app.tools.verification import (
    check_category_match,
    detect_fake_mark,
    verify_cml,
    verify_huid,
    verify_r_number,
)
from app.tools.web_rag import scrape_page, web_search_bis

logger = get_logger(__name__)
settings = get_settings()

# ── All tools the agent can call ──────────────────────────────────────────────

ALL_TOOLS = [
    search_bis_knowledge,
    verify_cml,
    verify_r_number,
    verify_huid,
    check_category_match,
    detect_fake_mark,
    web_search_bis,
    scrape_page,
    get_compliance_guide,
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Manak — the BIS Assistant. You know the Bureau of Indian Standards inside out: IS standards, ISI marks, hallmarking, CRS, product certification, compliance, and consumer safety in India.

You talk like a knowledgeable friend who happens to be a BIS expert — warm, direct, and clear. No corporate tone. No filler. If someone asks a simple question, give a simple answer. If it is complex, walk them through it step by step without overwhelming them.

---

HOW TO USE YOUR TOOLS (follow this order, do not skip steps):

1. Any informational question about BIS, standards, marks, or certification → search_bis_knowledge first. Always.
2. CM/L number present → verify_cml right away.
3. R-number present → verify_r_number right away.
4. HUID present → verify_huid right away.
5. verify_cml returned an IS number and the product type is known → run check_category_match.
6. User says "is this genuine / real / fake" → detect_fake_mark.
7. Use web_search_bis (then scrape_page on the best URL if needed) when: (a) search_bis_knowledge returned [no_results], or (b) the user asks for something specific (e.g. startup guidelines, a specific document, circular, amendment, scheme) that the retrieved chunks do not clearly address. If the retrieved knowledge does not directly answer the question, you must call web_search_bis before giving your final answer. Do not say you will search or that you "could not find" without actually calling the tool.
8. Queries about specific audiences (e.g. startups, MSMEs), specific documents, circulars, or amendments → if knowledge base results don't clearly cover it, call web_search_bis (then scrape_page if needed).
9. User shares a URL (BIS page or PDF) → scrape_page it and summarise.
10. "How do I get certified / what is the process" → get_compliance_guide.

---

HOW TO RESPOND:

Lead with the answer — not a preamble. Never start your reply with "I". If it is a yes/no, say yes or no first, then explain. If something is GENUINE or FAKE, say that in bold at the top.

Do not explain that you searched the knowledge base or the web; do not say "I did not find in the knowledge base so I searched…" or similar. Just state the answer and, if relevant, cite the source (e.g. "According to bis.gov.in…").

Keep it conversational:
- Short paragraphs, natural language
- Bullets only when listing multiple things — not for everything
- One clear sentence beats three vague ones
- Skip phrases like "Certainly!", "Great question!", "As an AI assistant..."

For verification results: open with **GENUINE** ✅ or **FAKE / NOT FOUND** ❌ or **SUSPICIOUS** ⚠️ — then the details.

For safety-critical products (gas cylinders, electrical cables, helmets, toys for kids): add a brief safety note. One line is enough.

If you found a source, mention it naturally: "According to bis.gov.in..." or link it as [BIS Hallmarking](https://www.bis.gov.in/hallmarking/).

If you could not verify something, say so plainly — never guess, never make up IS numbers or CM/L data.

---

WHAT YOU ARE (and are not):

You are Manak, the BIS Assistant. You help with BIS standards, product verification, hallmarking, certification processes, and consumer safety.

If someone asks about your model, architecture, or how you work as an AI — just say: "I'm Manak, here to help with all things BIS. What would you like to know — hallmarking, ISI marks, CRS, something else?"

If past conversation is included (labelled "Past conversation"), use it to understand context and answer questions like "what did I ask?" or "summarise our chat". Do not repeat or re-answer old messages — only answer the current question.

When "Retrieved BIS knowledge (pre-fetched)" is included in the user message, you already have search results. Do NOT call search_bis_knowledge again — use that content to answer. Only call web_search_bis (or scrape_page) if the pre-fetched block shows [no_results] or [vector_search_error], or the chunks clearly do not address the question. Calling search_bis_knowledge when results are already provided wastes time and duplicates results.

Use Indian English where it feels natural. Stay grounded in facts."""

# ── Graph singleton ───────────────────────────────────────────────────────────

_graph = None
_graph_key: tuple[str, str] | None = None


def _get_graph():
    global _graph, _graph_key
    key = (
        settings.llm_provider,
        settings.gemini_model if settings.llm_provider == "gemini" else settings.groq_model,
    )
    if _graph is None or _graph_key != key:
        llm = get_llm()
        _graph = create_react_agent(
            llm,
            tools=ALL_TOOLS,
            state_modifier=SYSTEM_PROMPT,
        )
        _graph_key = key
        logger.info("LangGraph ReAct agent compiled (provider=%s model=%s)", key[0], key[1])
    return _graph


# ── Tool call recorder ────────────────────────────────────────────────────────

def _record_tool_calls(messages: list[BaseMessage]) -> list[ToolCallRecord]:
    """Extract tool call records from message history for metadata."""
    records: list[ToolCallRecord] = []
    next_result_idx = 0
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                records.append(ToolCallRecord(
                    tool_name=tc["name"],
                    input_summary=_summarise_input(tc["name"], tc.get("args", {})),
                    outcome="success",
                    result_preview="",
                ))
        elif isinstance(msg, ToolMessage) and next_result_idx < len(records):
            content = str(msg.content)
            rec = records[next_result_idx]
            rec.result_preview = content[:120]
            if content.startswith("[not_found]") or content.startswith("[error]"):
                rec.outcome = "empty"
            elif content.startswith("[portal_unreachable]") or content.startswith("[scrape_error]"):
                rec.outcome = "error"
            next_result_idx += 1
    return records


# User-facing labels for tool status (short, live-site friendly)
TOOL_LABELS: dict[str, str] = {
    "search_bis_knowledge": "Searching standards & guides...",
    "verify_cml": "Verifying CM/L number...",
    "verify_r_number": "Verifying R-number...",
    "verify_huid": "Verifying HUID...",
    "check_category_match": "Checking product category...",
    "detect_fake_mark": "Checking mark authenticity...",
    "web_search_bis": "Searching official sources...",
    "scrape_page": "Loading page...",
    "get_compliance_guide": "Getting guide...",
    "pre_inject_rag": "Gathering information...",
}


def _summarise_input(tool_name: str, args: dict) -> str:
    if tool_name == "search_bis_knowledge":
        return f"query: {args.get('query', '')[:60]}"
    if tool_name in ("verify_cml", "verify_r_number", "verify_huid", "detect_fake_mark"):
        return str(list(args.values())[0])[:60] if args else ""
    if tool_name == "web_search_bis":
        return f"query: {args.get('query', '')[:60]}"
    if tool_name == "scrape_page":
        return args.get("url", "")[:80]
    if tool_name == "check_category_match":
        return f"{args.get('is_number', '')} vs {args.get('product_description', '')[:40]}"
    if tool_name == "get_compliance_guide":
        return args.get("product_type", "")[:60]
    return str(args)[:80]


def _log_agent_run(
    session_id: str,
    user_message: str,
    tool_calls: list[ToolCallRecord],
    sources: list[str],
    response_preview: str,
) -> None:
    """Log one structured line per request: input, tools used, tool response preview, sources."""
    input_preview = (user_message[:80] + "…") if len(user_message) > 80 else user_message
    tool_parts = []
    for tc in tool_calls:
        preview = (tc.result_preview[:60] + "…") if len(tc.result_preview) > 60 else tc.result_preview
        tool_parts.append(f"{tc.tool_name}({tc.input_summary}) → {tc.outcome}: {preview!r}")
    tools_str = " | ".join(tool_parts) if tool_parts else "no tools"
    sources_str = f", {len(sources)} source(s)" if sources else ""
    out_preview = (response_preview[:100] + "…") if len(response_preview) > 100 else response_preview
    logger.info(
        "agent_run session=%s input=%s tools=%s%s response_preview=%s",
        session_id[:8],
        input_preview,
        tools_str,
        sources_str,
        out_preview,
    )


def _extract_unverified(messages: list[BaseMessage]) -> list[UnverifiedItem]:
    """Detect what the agent tried but couldn't verify."""
    unverified = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            c = str(msg.content)
            if "[not_found]" in c:
                unverified.append(UnverifiedItem(
                    item=c.split("]")[1].strip()[:80],
                    reason="Not found in BIS database"
                ))
            elif "[portal_unreachable]" in c:
                unverified.append(UnverifiedItem(
                    item="BIS portal verification",
                    reason="Portal temporarily unreachable"
                ))
            elif "[no_results]" in c or "[no_web_results]" in c:
                unverified.append(UnverifiedItem(
                    item="Knowledge base search",
                    reason="No relevant documents found"
                ))
    return unverified


# ── Main streaming runner ─────────────────────────────────────────────────────
#
# How current query + past messages reach the LLM (LangGraph / LangChain style):
#
# 1. We build one HumanMessage with two clear sections so the model sees context
#    vs current query and does not echo:
#      - "Past conversation (for your context only):" + formatted history
#      - "Current user message:" + user_message
#    So the model gets a single user turn with structured context and one question to answer.
#
# 2. We pass that single message (or empty history → just current) as state:
#      graph.astream({"messages": input_messages})
#
# 3. LangGraph create_react_agent uses one "agent" node. Before calling the LLM it runs
#    the state_modifier: our SYSTEM_PROMPT (str) is turned into
#      [ SystemMessage(SYSTEM_PROMPT) ] + state["messages"]
#    So the LLM actually receives:
#      [ SystemMessage, HumanMessage, AIMessage, HumanMessage, ... , HumanMessage(current) ]
#
# 4. So: one HumanMessage contains past (for context) + current user message (to answer).


def _format_past_messages(messages: list[BaseMessage]) -> str:
    """Format past conversation for the 'context only' section of the user prompt."""
    lines: list[str] = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        if isinstance(m, HumanMessage):
            lines.append(f"User: {content}")
        elif isinstance(m, AIMessage):
            lines.append(f"Assistant: {content}")
    return "\n".join(lines) if lines else ""


def _build_user_prompt_with_context(
    past_messages: list[BaseMessage],
    current_user_message: str,
) -> str:
    """Build a single user prompt with past conversation (context) and current query."""
    current = current_user_message.strip()
    if not past_messages:
        return f"Current user message:\n{current}"
    context_block = _format_past_messages(past_messages)
    return (
        "Past conversation (for your context only):\n"
        f"{context_block}\n\n"
        "Current user message:\n"
        f"{current}"
    )


async def _run_pre_inject_retrieval(query: str) -> str:
    """
    Run only vector search before the first LLM call (pre-inject RAG).
    Web search is not run here — the agent calls web_search_bis / scrape_page when
    the retrieved chunks don't answer the question (e.g. [no_results] or niche topic).
    Saves tokens and Tavily calls when RAG is sufficient.
    """
    top_k = settings.pre_inject_rag_top_k
    try:
        return await search_bis_knowledge.ainvoke({"query": query, "top_k": top_k})
    except Exception as exc:
        logger.warning(f"[pre_inject] Vector search failed: {exc}")
        return "[vector_search_error] Pre-fetch failed."


def _build_user_prompt_with_injected_rag(
    past_messages: list[BaseMessage],
    current_user_message: str,
    vector_result: str,
) -> str:
    """Build user prompt with pre-fetched RAG context only (no web block)."""
    base = _build_user_prompt_with_context(past_messages, current_user_message)
    blocks = [
        "Retrieved BIS knowledge (pre-fetched; use this to answer if relevant):",
        vector_result,
        "",
        "---",
        "",
        base,
    ]
    return "\n".join(blocks)


async def run_agent(
    session_id: str,
    user_message: str,
    history: list[BaseMessage],
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Run the ReAct agent and yield SSE-ready dicts.
    When pre_inject_rag is True: runs vector search before the first LLM call and
    injects only RAG results. The agent calls web_search_bis when RAG is insufficient.
    Yields: {"type": "token"|"metadata"|"error"|"done"|"tool_status", ...}
    """
    graph = _get_graph()

    # Pre-inject RAG only (no web here — agent calls web_search_bis when needed)
    if settings.pre_inject_rag:
        pre_label = TOOL_LABELS.get("pre_inject_rag", "Gathering information...")
        yield {
            "type": "tool_status",
            "content": pre_label,
            "tool_status": {"tool": "pre_inject_rag", "status": "running", "message": pre_label},
        }
        vector_result = await _run_pre_inject_retrieval(user_message)
        yield {
            "type": "tool_status",
            "content": "",
            "tool_status": {"tool": "pre_inject_rag", "status": "done", "message": pre_label},
        }
        user_prompt = _build_user_prompt_with_injected_rag(history, user_message, vector_result)
    else:
        user_prompt = _build_user_prompt_with_context(history, user_message)

    input_messages: list[BaseMessage] = [HumanMessage(content=user_prompt)]

    full_response = ""
    all_messages: list[BaseMessage] = []
    iterations = 0
    hit_cap = False
    sources: list[str] = []
    pending_tool_names: list[str] = []
    pending_tool_index = 0

    try:
        # LangGraph streams events — we collect them and manage iteration count
        async for event in graph.astream(
            {"messages": input_messages},
            stream_mode="messages",
        ):
            # event is (message_chunk, metadata) in "messages" stream mode
            msg_chunk, meta = event if isinstance(event, tuple) else (event, {})

            # Count iterations — each AIMessage that contains tool_calls = 1 iteration
            if (
                isinstance(msg_chunk, AIMessage)
                and getattr(msg_chunk, "tool_calls", None)
            ):
                iterations += 1
                # Emit tool_status "running" for each tool the agent is about to run
                tool_calls = getattr(msg_chunk, "tool_calls", None) or []
                pending_tool_names = [tc.get("name", "") for tc in tool_calls if tc.get("name")]
                pending_tool_index = 0
                if pending_tool_names:
                    name = pending_tool_names[0]
                    label = TOOL_LABELS.get(name, f"Running {name}...")
                    yield {
                        "type": "tool_status",
                        "content": label,
                        "tool_status": {"tool": name, "status": "running", "message": label},
                    }
                if iterations >= settings.agent_max_iterations:
                    hit_cap = True
                    logger.warning(
                        f"Agent hit max iterations ({settings.agent_max_iterations}) "
                        f"for session {session_id}"
                    )
                    break

            # Collect all messages for metadata extraction
            all_messages.append(msg_chunk)

            # When a tool finishes: emit "done", then "running" for next tool if any
            if isinstance(msg_chunk, ToolMessage) and pending_tool_names:
                if pending_tool_index < len(pending_tool_names):
                    name = pending_tool_names[pending_tool_index]
                    label = TOOL_LABELS.get(name, name)
                    yield {
                        "type": "tool_status",
                        "content": "",
                        "tool_status": {"tool": name, "status": "done", "message": label},
                    }
                    pending_tool_index += 1
                    if pending_tool_index < len(pending_tool_names):
                        next_name = pending_tool_names[pending_tool_index]
                        next_label = TOOL_LABELS.get(next_name, f"Running {next_name}...")
                        yield {
                            "type": "tool_status",
                            "content": next_label,
                            "tool_status": {"tool": next_name, "status": "running", "message": next_label},
                        }

            # Stream final text tokens (only from the last AI response — no tool calls)
            if (
                isinstance(msg_chunk, AIMessage)
                and not getattr(msg_chunk, "tool_calls", None)
                and msg_chunk.content
            ):
                token = str(msg_chunk.content)
                full_response += token
                yield {"type": "token", "content": token}

            # Collect source URLs from ToolMessages
            if isinstance(msg_chunk, ToolMessage):
                content = str(msg_chunk.content)
                # Extract URLs from tool results
                import re
                for url in re.findall(r"https?://[^\s\n]+", content):
                    if settings.is_domain_allowed(url) and url not in sources:
                        sources.append(url)

    except AgentError:
        raise
    except Exception as exc:
        logger.exception(f"Agent error for session {session_id}: {exc}")
        yield {"type": "error", "content": f"Agent encountered an error: {str(exc)}"}
        return

    # If we hit the cap and have no response yet, generate a partial answer
    if hit_cap and not full_response:
        partial_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"NOTE: You have reached the maximum tool call limit. "
            f"Generate the best partial answer you can from what you have gathered so far. "
            f"Clearly state at the end what you could not verify."
        )
        try:
            llm = get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=partial_prompt),
                *history,
                HumanMessage(content=user_message),
            ])
            full_response = str(response.content)
            for token in full_response.split(" "):
                yield {"type": "token", "content": token + " "}
        except Exception as exc:
            yield {
                "type": "token",
                "content": "I was unable to complete the full verification within the allowed steps. Please try a more specific question."
            }

    # Build metadata
    tool_calls = _record_tool_calls(all_messages)
    unverified = _extract_unverified(all_messages)

    metadata = ResponseMetadata(
        tool_calls=tool_calls,
        sources=sources[:10],
        unverified=unverified,
        iterations_used=iterations,
        hit_max_iterations=hit_cap,
    )

    # Structured log: input, tools used, and how response was built (RAG vs web etc.)
    _log_agent_run(session_id, user_message, tool_calls, sources, full_response)

    yield {"type": "metadata", "content": "", "metadata": metadata.model_dump()}
    yield {"type": "done", "content": ""}