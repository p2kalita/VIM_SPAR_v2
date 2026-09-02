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


def stream_vllm_llm(
    endpoint: str,
    model_name: str,
    messages: list[dict],
    system_instruction: str,
    user_query: str,
    api_key: str = "EMPTY",
    timeout: int = 60,
) -> Generator[str, None, None]:
    """
    Stream tokens from a vLLM OpenAI-compatible server (e.g. Colab + Ngrok or local vLLM).
    """
    import json
    import requests

    # Normalize endpoint URL to chat/completions
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        chat_url = base
    elif base.endswith("/v1"):
        chat_url = f"{base}/chat/completions"
    else:
        chat_url = f"{base}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'EMPTY'}",
        "ngrok-skip-browser-warning": "true",  # Bypass Ngrok free tier browser warning
    }

    # Format multi-turn message history for OpenAI chat format
    formatted_messages = [{"role": "system", "content": system_instruction}]

    # Include recent prior conversation turns
    history = messages[:-1][-_MAX_HISTORY_TURNS:] if len(messages) > 1 else []
    for msg in history:
        r = msg.get("role", "user")
        c = msg.get("content", "").strip()
        if c:
            formatted_messages.append({"role": "assistant" if r == "assistant" else "user", "content": c})

    formatted_messages.append({"role": "user", "content": user_query})

    resolved_model = model_name or os.getenv("VLLM_MODEL", "/content/models/Qwen2.5-3B-Instruct")

    payload = {
        "model": resolved_model,
        "messages": formatted_messages,
        "stream": True,
        "temperature": 0.2,
        "max_tokens": 1500,
    }

    logger.info("[RAG-VLLM] Initiating streaming request to %s (model='%s', msgs=%d)",
                chat_url, resolved_model, len(formatted_messages))

    try:
        # First attempt with resolved_model
        response = requests.post(chat_url, json=payload, headers=headers, stream=True, timeout=timeout)
        
        # If model name mismatch (e.g. user typed 'Qwen2.5-3B-Instruct' but server has '/content/models/...'), auto-discover
        if response.status_code == 404 or (response.status_code == 400 and 'does not exist' in response.text):
            try:
                models_url = chat_url.replace('/chat/completions', '/models')
                m_resp = requests.get(models_url, headers=headers, timeout=5)
                if m_resp.status_code == 200:
                    m_data = m_resp.json().get('data', [])
                    if m_data and 'id' in m_data[0]:
                        auto_model = m_data[0]['id']
                        logger.info("[RAG-VLLM] Auto-detected active server model: '%s'", auto_model)
                        payload['model'] = auto_model
                        response = requests.post(chat_url, json=payload, headers=headers, stream=True, timeout=timeout)
            except Exception as disc_err:
                logger.debug("[RAG-VLLM] Auto-model discovery skipped: %s", disc_err)

        if response.status_code != 200:
            err_text = response.text[:250]
            logger.error("[RAG-VLLM] vLLM server returned HTTP %d: %s", response.status_code, err_text)
            yield f"⚠️ vLLM server returned HTTP {response.status_code}: {err_text}"
            return

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk_json = json.loads(data_str)
                    choices = chunk_json.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        token = delta.get("content")
                        if token:
                            yield token
                except Exception as parse_err:
                    logger.debug("[RAG-VLLM] JSON parse skip: %s", parse_err)

    except requests.exceptions.ConnectionError as conn_err:
        logger.error("[RAG-VLLM] Connection to vLLM server failed: %s", conn_err)
        yield f"⚠️ Failed to connect to vLLM endpoint at `{chat_url}`. Please verify your Colab / Ngrok tunnel is active."
    except Exception as e:
        logger.error("[RAG-VLLM] Unexpected error during vLLM streaming: %s", e)
        yield f"⚠️ vLLM stream error: {str(e)}"


def stream_rag_chat(
    messages: list[dict],
    filter_doc_id: Optional[str] = None,
    writing_style: str = "default",
    citations: bool = False,
    custom_model: Optional[str] = None,
    custom_endpoint: Optional[str] = None,
    vllm_api_key: Optional[str] = None,
    provider: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Stream SSE tokens for the AI Chat UI.
    Retrieves context from ChromaDB and streams response via vLLM (Colab/Ngrok) or Gemini.
    """
    import os
    start_time = time.time()

    # Extract latest user message
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_query = msg.get("content", "").strip()
            break

    logger.info("[RAG-CHAT] Incoming chat query: '%s' (history_msgs=%d, style='%s', citations=%s, filter_doc='%s', provider='%s')",
                user_query[:120], len(messages), writing_style, citations, filter_doc_id, provider)

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

    # Build conversation history for multi-turn context
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
        f"RETRIEVED INVOICE CONTEXT:\n{context_text}\n"
    )

    # Determine LLM backend (vLLM vs Gemini)
    vllm_url = custom_endpoint or os.getenv("VLLM_BASE_URL", "").strip()
    effective_provider = provider or os.getenv("LLM_PROVIDER", "").strip().lower()

    use_vllm = False
    is_gemini_model = custom_model.startswith("gemini-") if custom_model else False
    if effective_provider in ("vllm", "openai") or (vllm_url and not is_gemini_model):
        use_vllm = bool(vllm_url)
    elif vllm_url and ("qwen" in (custom_model or "").lower() or "llama" in (custom_model or "").lower()):
        use_vllm = True

    # ── BRANCH 1: vLLM Streaming (Colab / Ngrok / OpenAI-compatible) ───────────
    if use_vllm:
        model_name = custom_model or os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-3B-Instruct")
        api_key = vllm_api_key or os.getenv("VLLM_API_KEY", "EMPTY")
        logger.info("[RAG-CHAT] Routing stream to vLLM at '%s' (model='%s')", vllm_url, model_name)

        token_count = 0
        try:
            for token in stream_vllm_llm(
                endpoint=vllm_url,
                model_name=model_name,
                messages=messages,
                system_instruction=system_instruction,
                user_query=user_query,
                api_key=api_key,
            ):
                token_count += 1
                clean_text = token.replace("\n", "\\n")
                yield f"data: {clean_text}\n\n"

            if citations and chunks:
                cit_text = "\n\n---\n**📌 Citations & Sources:**\n"
                for i, c in enumerate(chunks, 1):
                    cit_text += f"- **Source {i}**: Invoice `{c.get('invoice_number', 'N/A')}` ({c.get('filename', 'Doc')}) [Match: {int((1-c.get('distance', 0))*100)}%]\n"
                clean_cit = cit_text.replace("\n", "\\n")
                yield f"data: {clean_cit}\n\n"

            elapsed = time.time() - start_time
            logger.info("[RAG-CHAT] vLLM stream completed in %.2fs (%d tokens)", elapsed, token_count)
            yield "data: [DONE]\n\n"
            return

        except Exception as e:
            logger.error("[RAG-CHAT] vLLM streaming failed: %s", e)
            if chunks:
                fallback_msg = (
                    f"⚠️ *vLLM connection issue ({e}). Extracted invoice context:*\n\n"
                )
                for i, c in enumerate(chunks, 1):
                    fallback_msg += f"**[{c.get('invoice_number')}]** ({c.get('filename')}):\n> {c.get('text')}\n\n"
                clean_fb = fallback_msg.replace("\n", "\\n")
                yield f"data: {clean_fb}\n\n"
            else:
                yield f"data: ⚠️ Error from vLLM server: {str(e)}\n\n"
            yield "data: [DONE]\n\n"
            return

    # ── BRANCH 2: Google Gemini Streaming ──────────────────────────────────────
    prompt = f"{system_instruction}\n\n{conversation_context}USER QUESTION:\n{user_query}"
    model_name = custom_model or GEMINI_MODEL or "gemini-3.5-flash"
    if "/" in model_name:
        model_name = model_name.split("/")[-1]
    if not model_name.startswith("gemini-"):
        model_name = "gemini-3.5-flash"

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
