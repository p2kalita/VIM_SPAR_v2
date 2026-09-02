import time
from typing import Generator, Optional

import google.generativeai as genai
from crewai import Agent, Crew, Process, Task
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from vim_logger import get_logger

from vim.rag.store import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    TOP_K,
    format_chunks_for_context,
    retrieve_chunks,
)

logger = get_logger("vim.rag.chat")

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("[RAG-CHAT] Gemini client configured with active API key.")
else:
    logger.warning("[RAG-CHAT] GEMINI_API_KEY is not set in environment.")


# ── Maximum conversation history turns to include in the prompt ────────────────
_MAX_HISTORY_TURNS = 10


def build_synthesis_agent() -> Agent:
    logger.debug("[RAG-CHAT] Building CrewAI synthesis agent for model='gemini/%s'", GEMINI_MODEL)
    return Agent(
        role="SPAR Invoice Intelligence Analyst",
        goal="Provide accurate, concise, and grounded answers using retrieved invoice document data.",
        backstory="You are an expert financial document AI assistant built for SPAR Infosys Invoice Management.",
        tools=[],
        llm=f"gemini/{GEMINI_MODEL}",
        verbose=False,
        allow_delegation=False,
    )


def build_synthesis_task(agent: Agent, query: str, context_text: str = "") -> Task:
    return Task(
        description=(
            f"You are analyzing invoice documents for SPAR Vendor Invoice Management.\n\n"
            f"RETRIEVED INVOICE CONTEXT:\n"
            f"{context_text}\n\n"
            f"USER QUESTION:\n"
            f"{query}\n\n"
            f"Instructions:\n"
            f"- Answer the user's question accurately using ONLY the retrieved invoice context above.\n"
            f"- Cite the relevant invoice number(s) or document filenames when available.\n"
            f"- If the required information is not found in the context, state clearly: 'No matching invoice records were found in the uploaded documents.'"
        ),
        expected_output="A clear, professional, and grounded answer citing relevant invoice numbers.",
        agent=agent,
    )


class QueryCrew:
    """Non-streaming query synthesis compatible with standard CrewAI execution."""

    def __init__(self) -> None:
        self._agent = build_synthesis_agent()

    def run(
        self,
        query: str,
        invoice_number: Optional[str] = None,
        tenant_id: Optional[str] = None,
        top_k: int = TOP_K,
    ) -> str:
        start_t = time.time()
        logger.info("[RAG-CREW] Executing CrewAI synthesis for query='%s', filter_inv='%s'",
                    query[:100], invoice_number)

        chunks = retrieve_chunks(query=query, invoice_number=invoice_number, top_k=top_k)
        context_text = format_chunks_for_context(chunks)
        task = build_synthesis_task(self._agent, query=query, context_text=context_text)
        crew = Crew(agents=[self._agent], tasks=[task], process=Process.sequential, verbose=False)

        try:
            for attempt in Retrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential_jitter(initial=1, max=4),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            ):
                with attempt:
                    result = str(crew.kickoff())
                    elapsed = time.time() - start_t
                    logger.info("[RAG-CREW] CrewAI synthesis finished in %.2fs (result_len=%d)", elapsed, len(result))
                    return result
        except Exception as e:
            logger.warning("[RAG-CREW] CrewAI execution fallback triggered: %s", e)
            if chunks:
                best = chunks[0]
                return (
                    f"**Direct Invoice Match ({best.get('invoice_number', 'Doc')}):**\n\n"
                    f"{best.get('text', '')}"
                )

        # BUG 6 fix: safety fallback if Retrying loop completes without returning
        return "No matching invoice details found."


def _build_conversation_context(messages: list[dict]) -> str:
    """
    BUG 4 fix: Format conversation history into a multi-turn context string
    so the LLM has awareness of prior turns.
    Only includes the last _MAX_HISTORY_TURNS messages (excluding the latest user message).
    """
    if len(messages) <= 1:
        return ""

    # Exclude the latest user message (it's used as the primary query)
    history = messages[:-1][-_MAX_HISTORY_TURNS:]
    if not history:
        return ""

    lines = []
    for msg in history:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")

    if not lines:
        return ""

    return "CONVERSATION HISTORY (for context):\n" + "\n".join(lines) + "\n\n"


def stream_rag_chat(
    messages: list[dict],
    filter_doc_id: Optional[str] = None,
    writing_style: str = "default",
    citations: bool = False,
    custom_model: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Stream SSE tokens for the AI Chat UI.
    Retrieves context from ChromaDB and streams response via Gemini (or fallback).
    """
    start_time = time.time()

    # Extract latest user message
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_query = msg.get("content", "").strip()
            break

    logger.info("[RAG-CHAT] Incoming chat query: '%s' (history_msgs=%d, style='%s', citations=%s, filter_doc='%s')",
                user_query[:120], len(messages), writing_style, citations, filter_doc_id)

    if not user_query:
        logger.warning("[RAG-CHAT] Empty user query received.")
        yield "data: Please provide a question or instruction.\n\n"
        yield "data: [DONE]\n\n"
        return

    # Retrieve relevant chunks from unified ChromaDB collection
    try:
        chunks = retrieve_chunks(query=user_query, invoice_number=filter_doc_id, top_k=TOP_K)
        logger.info("[RAG-CHAT] Context retrieved: %d chunks found for query", len(chunks))
    except Exception as e:
        logger.error("[RAG-CHAT] Error retrieving chunks from vector store: %s", e)
        chunks = []

    context_text = format_chunks_for_context(chunks)

    # BUG 4 fix: Build conversation history for multi-turn context
    conversation_context = _build_conversation_context(messages)

    # Style tone instruction
    style_prompt = ""
    if writing_style == "formal":
        style_prompt = "Style: Strict, professional financial auditor tone."
    elif writing_style == "casual":
        style_prompt = "Style: Conversational, friendly, and approachable."
    elif writing_style == "creative":
        style_prompt = "Style: Engaging and elaborative while preserving strict numeric accuracy."

    citation_prompt = ""
    if citations:
        citation_prompt = "Include a '### 📌 Sources & Citations' section at the end referencing the source invoice number(s) and similarity score."

    system_instruction = (
        "You are the SPAR AI Invoice Assistant, an in-house expert AI analyzing vendor invoices and accounts payable documents.\n"
        "Ground your answers strictly on the retrieved invoice context below.\n"
        "If the information is not present in the context, inform the user clearly and politely.\n\n"
        f"{style_prompt}\n"
        f"{citation_prompt}\n\n"
        f"{conversation_context}"
        f"RETRIEVED INVOICE CONTEXT:\n{context_text}\n"
    )

    prompt = f"{system_instruction}\n\nUSER QUESTION:\n{user_query}"

    model_name = custom_model or GEMINI_MODEL or "gemini-2.5-flash"
    if "/" in model_name:
        model_name = model_name.split("/")[-1]
    if not model_name.startswith("gemini-"):
        model_name = "gemini-2.5-flash"

    logger.info("[RAG-CHAT] Initiating Gemini stream with model='%s' (context_chars=%d, history_turns=%d)",
                model_name, len(context_text), len(messages) - 1)

    try:
        if GEMINI_API_KEY:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt, stream=True)
            token_count = 0
            for chunk in response:
                if chunk.text:
                    token_count += 1
                    clean_text = chunk.text.replace("\n", "\\n")
                    yield f"data: {clean_text}\n\n"

            if citations and chunks:
                cit_text = "\n\n---\n**📌 Citations & Sources:**\n"
                for i, c in enumerate(chunks, 1):
                    cit_text += f"- **Source {i}**: Invoice `{c.get('invoice_number', 'N/A')}` ({c.get('filename', 'Doc')}) [Match: {int((1-c.get('distance', 0))*100)}%]\n"
                clean_cit = cit_text.replace("\n", "\\n")
                yield f"data: {clean_cit}\n\n"

            elapsed = time.time() - start_time
            logger.info("[RAG-CHAT] Stream completed successfully in %.2fs (%d stream tokens emitted)",
                        elapsed, token_count)
            yield "data: [DONE]\n\n"
            return
        else:
            logger.error("[RAG-CHAT] Stream aborted: GEMINI_API_KEY missing.")
            yield "data: ⚠️ GEMINI_API_KEY is not configured in .env.\n\n"
            yield "data: [DONE]\n\n"
            return

    except Exception as e:
        logger.error("[RAG-CHAT] Gemini streaming error: %s", e)
        # Fallback direct response from chunks if available
        if chunks:
            logger.info("[RAG-CHAT] Emitting direct chunk fallback due to LLM error.")
            fallback_msg = (
                "⚠️ *Streaming encountered a temporary limit. Here is the direct extracted data:*\n\n"
                f"**Top Extracted Invoice Chunks:**\n\n"
            )
            for i, c in enumerate(chunks, 1):
                fallback_msg += f"**[{c.get('invoice_number')}]** ({c.get('filename')}):\n> {c.get('text')}\n\n"
            clean_fb = fallback_msg.replace("\n", "\\n")
            yield f"data: {clean_fb}\n\n"
        else:
            yield f"data: ⚠️ Error generating response: {str(e)}\n\n"
        yield "data: [DONE]\n\n"
