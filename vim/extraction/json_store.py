"""Read/write enriched extraction records to output/enriched.json."""
 
import json
import os
import threading
from pathlib import Path
 
from vim.extraction import config
from vim_logger import get_logger

logger = get_logger("vim.extraction.json_store")

ENRICHED_PATH = config.OUTPUT_DIR / "enriched.json"
 
# Every update is a read-modify-write of the whole file, and uploads are now
# processed in parallel, so unsynchronised callers would drop each other's
# records. Reentrant because the update helpers call save_all() while holding it.
_file_lock = threading.RLock()
 
 
def load_all() -> list[dict]:
    with _file_lock:
        if not ENRICHED_PATH.exists():
            logger.debug("[JSON_STORE] %s does not exist, returning empty list", ENRICHED_PATH)
            return []
        try:
            data = json.loads(ENRICHED_PATH.read_text(encoding="utf-8"))
            records = data if isinstance(data, list) else []
            logger.debug("[JSON_STORE] Loaded %d record(s) from %s", len(records), ENRICHED_PATH)
            return records
        except Exception as e:
            logger.error("[JSON_STORE] Error loading JSON from %s: %s", ENRICHED_PATH, e, exc_info=True)
            return []
 
 
def save_all(records: list[dict]) -> Path:
    from vim.extraction.vendors import attach_vendor_id
 
    with _file_lock:
        ENRICHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        enriched = [attach_vendor_id(dict(rec)) for rec in records]
        payload = json.dumps(enriched, indent=2, default=str)
 
        # Write then rename, so an interrupted write cannot leave the file
        # truncated and unreadable for every later upload.
        tmp_path = ENRICHED_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, ENRICHED_PATH)
        logger.info("[JSON_STORE] Saved %d record(s) to %s", len(enriched), ENRICHED_PATH)
 
    return ENRICHED_PATH
 
 
def _record_key(record: dict) -> str | None:
    """Stable key for one upload.

    stored_file_name is unique per upload (uuid + original name). Keying by
    original file_name collapsed a bulk batch of similarly named files onto
    one JSON record, so validation only ran for the last file.
    """
    return record.get("stored_file_name") or record.get("file_name") or record.get("file_path")
 
 
def _dedupe_records(records: list[dict]) -> list[dict]:
    """Keep the latest record per file_name; preserve first-seen order."""
    ordered_keys: list[str] = []
    by_key: dict[str, dict] = {}
    orphans: list[dict] = []
 
    for rec in records:
        key = _record_key(rec)
        if not key:
            orphans.append(rec)
            continue
        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = rec
 
    return [by_key[k] for k in ordered_keys] + orphans
 
 
def delete_record(key: str, stored_file_name: str | None = None) -> bool:
    """
    Remove the record for a file.
 
    Records are keyed by original filename, so pass stored_file_name to delete
    only the record for one specific upload. Without it, re-uploading a name
    that already exists would delete the earlier file's record too.
    """
    def is_target(rec: dict) -> bool:
        if stored_file_name is not None:
            return rec.get("stored_file_name") == stored_file_name
        return _record_key(rec) == key
 
    with _file_lock:
        records = _dedupe_records(load_all())
        remaining = [r for r in records if not is_target(r)]
        if len(remaining) == len(records):
            logger.debug("[JSON_STORE] delete_record: key='%s', stored='%s' not found", key, stored_file_name)
            return False
        save_all(remaining)
        logger.info("[JSON_STORE] Deleted record key='%s', stored='%s'", key, stored_file_name)
    return True
 
 
def find_by_stored_name(stored_file_name: str) -> dict | None:
    """Return the enriched.json record for one upload on disk, if any."""
    if not stored_file_name:
        return None
    for rec in load_all():
        if rec.get("stored_file_name") == stored_file_name:
            logger.debug("[JSON_STORE] Found record for stored_file_name='%s'", stored_file_name)
            return rec
    logger.debug("[JSON_STORE] No record found for stored_file_name='%s'", stored_file_name)
    return None
 
 
def upsert_record(record: dict) -> Path:
    """Insert or update one record in enriched.json (matched by stored file)."""
    key = _record_key(record)
    logger.debug("[JSON_STORE] Upserting record key='%s' (status='%s')", key, record.get("status"))
 
    with _file_lock:
        records = _dedupe_records(load_all())
 
        if not key:
            records.append(record)
            logger.info("[JSON_STORE] Appended unkeyed record (total=%d)", len(records))
            return save_all(records)
 
        updated = False
        for i, existing in enumerate(records):
            if _record_key(existing) == key:
                records[i] = record
                updated = True
                logger.debug("[JSON_STORE] Updated existing record for key='%s'", key)
                break
        if not updated:
            records.append(record)
            logger.debug("[JSON_STORE] Appended new record for key='%s'", key)
        return save_all(records)
