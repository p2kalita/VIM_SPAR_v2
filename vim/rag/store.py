import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from vim_logger import get_logger

logger = get_logger("vim.rag.store")

# Load .env from the project root (two levels up from vim/rag/)
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

# ── Configuration ──────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", str(_ROOT / "chroma_db"))
CHROMA_COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "invoices_rag")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "BAAI/bge-small-en-v1.5")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
TOP_K = int(os.getenv("TOP_K", "4"))

INVOICE_KEY = "invoice_number"
FILENAME_KEY = "filename"


# ── ChromaDB Client & Collection (BUG 2 fix: separate client cache) ───────────
@lru_cache(maxsize=1)
def _get_chroma_client() -> chromadb.ClientAPI:
    """Return a single shared PersistentClient to prevent SQLite locking issues."""
    logger.info("[RAG-STORE] Creating PersistentClient at path='%s'", CHROMA_PERSIST_DIR)
    return chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)


@lru_cache(maxsize=1)
def _get_embedding_function():
    """Cache the SentenceTransformer model to avoid reloading on every call."""
    logger.info("[RAG-STORE] Loading embedding model='%s'", EMBED_MODEL_NAME)
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL_NAME)


@lru_cache(maxsize=1)
def get_collection(name: str = CHROMA_COLLECTION_NAME) -> chromadb.Collection:
    """Initialize ChromaDB collection using the shared client and embedding function."""
    logger.info("[RAG-STORE] Initializing ChromaDB collection='%s'", name)
    client = _get_chroma_client()
    ef = _get_embedding_function()
    collection = client.get_or_create_collection(
        name=name,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info("[RAG-STORE] ChromaDB collection ready. Total existing items: %d", collection.count())
    return collection


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character-level chunks."""
    text = text.strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        chunk = text[start: start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        start += max(1, chunk_size - chunk_overlap)
    logger.debug("[RAG-STORE] Text split into %d chunks (size=%d, overlap=%d)", len(chunks), chunk_size, chunk_overlap)
    return chunks


def ingest_invoice(
    invoice_number: str,
    text: str,
    filename: str = "",
    tenant_id: str = "",
    extra_meta: Optional[dict] = None,
) -> int:
    """
    Chunk and index an invoice into ChromaDB.
    All invoices are indexed into the unified RAG collection.
    """
    inv_num = invoice_number or (Path(filename).stem if filename else "INV-DOC")
    file_name = filename or f"{inv_num}.pdf"
    logger.info("[RAG-STORE] Ingesting invoice document: inv_num='%s', file='%s', text_len=%d",
                inv_num, file_name, len(text))

    chunks = chunk_text(text)
    if not chunks:
        logger.warning("[RAG-STORE] No chunks generated for file='%s' (empty text)", file_name)
        return 0

    documents, metadatas, ids = [], [], []

    for i, chunk in enumerate(chunks):
        documents.append(chunk)
        meta = {
            INVOICE_KEY: inv_num,
            FILENAME_KEY: file_name,
            "chunk_index": i,
        }
        if extra_meta:
            meta.update(extra_meta)
        metadatas.append(meta)
        ids.append(str(uuid.uuid5(uuid.NAMESPACE_OID, f"{inv_num}::{file_name}::{i}")))

    collection = get_collection()
    collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
    logger.info("[RAG-STORE] Successfully indexed %d chunks for invoice='%s' (total in collection: %d)",
                len(chunks), inv_num, collection.count())
    return len(chunks)


def retrieve_chunks(
    query: str,
    invoice_number: Optional[str] = None,
    top_k: int = TOP_K,
    tenant_id: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve top-k chunks across all uploaded invoices.
    Optionally filter by a specific invoice_number if requested.
    """
    collection = get_collection()
    count = collection.count()
    if count == 0:
        logger.warning("[RAG-STORE] Retrieval attempted but collection is empty.")
        return []

    where = None
    if invoice_number and invoice_number.strip():
        where = {INVOICE_KEY: {"$eq": invoice_number.strip()}}
        logger.debug("[RAG-STORE] Query filter applied: invoice_number='%s'", invoice_number.strip())

    n_results = min(top_k, count)
    logger.info("[RAG-STORE] Querying vector store: query='%s' (top_k=%d, total_items=%d, filter=%s)",
                query[:100], n_results, count, where)

    result = collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    ids = (result.get("ids") or [[]])[0]

    retrieved = [
        {
            "text": doc,
            "invoice_number": (meta or {}).get(INVOICE_KEY, ""),
            "filename": (meta or {}).get(FILENAME_KEY, ""),
            "chunk_index": (meta or {}).get("chunk_index", -1),
            "distance": dist,
            "id": cid,
        }
        for doc, meta, dist, cid in zip(docs, metas, distances, ids)
    ]

    if retrieved:
        best_sim = 1 - retrieved[0]["distance"] if retrieved[0]["distance"] is not None else 0
        logger.info("[RAG-STORE] Retrieved %d chunks (top similarity: %.3f, top invoice: '%s')",
                    len(retrieved), best_sim, retrieved[0].get("invoice_number"))
    else:
        logger.info("[RAG-STORE] No matching chunks found for query.")

    return retrieved


def format_chunks_for_context(chunks: list[dict]) -> str:
    """Format retrieved chunks into a clean prompt context string."""
    if not chunks:
        return "No relevant invoice content was found for this query in the vector store."
    return "\n\n---\n\n".join(
        f"[Source Chunk {i} | Invoice: {c.get('invoice_number', 'N/A')} | File: {c.get('filename', 'N/A')} | Similarity: {1 - c.get('distance', 0):.3f}]\n{c.get('text', '')}"
        for i, c in enumerate(chunks, 1)
    )


def get_store_stats() -> dict:
    """
    Return unified collection statistics and stored invoices.
    BUG 3 fix: Use paginated metadata retrieval instead of loading everything at once.
    """
    collection = get_collection()
    count = collection.count()
    if count == 0:
        return {
            "chunk_count": 0,
            "invoice_count": 0,
            "invoices": [],
            "total_chunks": 0,
        }

    # Paginated metadata scan to avoid loading entire collection into memory
    invoice_set: set[str] = set()
    batch_size = 1000
    offset = 0

    while offset < count:
        batch = collection.get(
            include=["metadatas"],
            limit=batch_size,
            offset=offset,
        )
        batch_metas = batch.get("metadatas") or []
        if not batch_metas:
            break
        for m in batch_metas:
            if m and m.get(INVOICE_KEY):
                invoice_set.add(m[INVOICE_KEY])
        offset += len(batch_metas)

    invoices = sorted(invoice_set)
    logger.debug("[RAG-STORE] Store stats: count=%d, unique_invoices=%d", count, len(invoices))
    return {
        "chunk_count": count,
        "invoice_count": len(invoices),
        "invoices": invoices,
        "total_chunks": count,
    }


def get_vendor_data(vendor_id: str = "") -> dict:
    """Backward compatibility helper for old views."""
    return get_store_stats()
