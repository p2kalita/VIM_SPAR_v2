"""Connect a Gmail inbox and turn invoice attachments into extraction jobs."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from vim.extraction import config as extraction_config
from vim.timezone import get_ist_now
from vim_logger import get_logger

logger = get_logger("vim.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_MIN_IMAGE_BYTES = 20 * 1024
_MAX_MESSAGES_PER_SYNC = 20
_ingest_lock = threading.Lock()


def _reload_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(extraction_config.ENV_PATH, override=True)
    except Exception:
        pass


def oauth_client_id() -> str:
    _reload_env()
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def oauth_client_secret() -> str:
    _reload_env()
    return (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()


def oauth_configured() -> bool:
    return bool(oauth_client_id() and oauth_client_secret())


def gmail_api_enable_url() -> str:
    project = oauth_client_id().split("-", 1)[0]
    return (
        "https://console.cloud.google.com/apis/api/gmail.googleapis.com/overview"
        + (f"?project={project}" if project else "")
    )


def friendly_sync_error(error) -> str:
    text = str(error or "")
    if "accessNotConfigured" in text or "Gmail API has not been used" in text:
        return (
            "Gmail API is not enabled on this Google Cloud project. "
            "Open the Gmail API page, click Enable, wait a minute, then Sync now."
        )
    if "access_denied" in text:
        return "Google denied access. Add this mailbox as an OAuth test user, then reconnect."
    if "redirect_uri" in text.lower():
        return "Redirect URI mismatch. Add the callback URL on the OAuth Web client, then reconnect."
    compact = " ".join(text.split())
    return compact[:500]


def default_search_query() -> str:
    clauses = " OR ".join(
        f"filename:{ext.lstrip('.')}"
        for ext in extraction_config.SUPPORTED_EXTENSIONS
    )
    return f"in:inbox has:attachment ({clauses})"


def default_poll_seconds() -> int:
    raw = (os.getenv("GMAIL_POLL_SECONDS") or "60").strip()
    try:
        return max(30, min(int(raw), 3600))
    except ValueError:
        return 60


def redirect_uri_from_request() -> str:
    configured = (os.getenv("GMAIL_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    from flask import url_for
    return url_for("admin_gmail_callback", _external=True)


def _allow_http_localhost(redirect_uri: str) -> None:
    if redirect_uri.startswith("http://127.0.0.1") or redirect_uri.startswith("http://localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def _client_config(redirect_uri: str) -> dict:
    if not oauth_configured():
        raise EnvironmentError(
            "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are not set. "
            "Add them to .env from a Google Cloud OAuth Web client."
        )
    return {
        "web": {
            "client_id": oauth_client_id(),
            "client_secret": oauth_client_secret(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def build_authorize_url(redirect_uri: str) -> tuple[str, str]:
    from google_auth_oauthlib.flow import Flow

    _allow_http_localhost(redirect_uri)
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    row = get_account()
    auth_kwargs = {
        "access_type": "offline",
        "prompt": "consent",
    }
    if row and row.Email:
        auth_kwargs["login_hint"] = row.Email
    url, state = flow.authorization_url(**auth_kwargs)
    return url, state


def exchange_code(redirect_uri: str, authorization_response: str) -> dict:
    from google_auth_oauthlib.flow import Flow

    _allow_http_localhost(redirect_uri)
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    email = _email_from_credentials(creds)
    return {"token_json": creds.to_json(), "email": email}


def _email_from_credentials(creds) -> str:
    try:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        return (profile.get("emailAddress") or "").strip()
    except Exception as e:
        logger.warning("[GMAIL] Could not read mailbox address: %s", e)
        return ""


def get_account():
    from vim_database.models import GmailAccount

    return GmailAccount.query.order_by(GmailAccount.AccountID.asc()).first()


def save_connection(token_json: str, email: str, user_id=None):
    from vim_database.database import db, write_lock
    from vim_database.models import GmailAccount

    with write_lock:
        row = GmailAccount.query.order_by(GmailAccount.AccountID.asc()).first()
        if row is None:
            row = GmailAccount()
            db.session.add(row)
        row.Email = email or row.Email
        row.TokenJson = token_json
        row.Enabled = True
        row.SearchQuery = row.SearchQuery or default_search_query()
        row.PollSeconds = row.PollSeconds or default_poll_seconds()
        row.ConnectedAt = get_ist_now()
        row.ConnectedByUserID = user_id
        row.AfterInternalMs = str(int(time.time() * 1000))
        row.LastError = None
        db.session.commit()
    logger.info("[GMAIL] Connected mailbox '%s' — ignoring mail before now", email)
    return row


def ignore_mail_before_now() -> None:
    """Do not ingest anything already sitting in the inbox."""
    from vim_database.database import db, write_lock

    with write_lock:
        row = get_account()
        if row is None:
            return
        row.AfterInternalMs = str(int(time.time() * 1000))
        db.session.commit()
    logger.info("[GMAIL] Watermark set to now — older inbox mail will be ignored")


def _query_since_watermark(query: str, after_ms: int) -> str:
    """Keep Gmail from listing the historical inbox; same-day mail is still filtered by ms."""
    query = (query or "").strip()
    if not after_ms:
        return query
    if "after:" in query.lower() or "newer_than:" in query.lower():
        return query
    day = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc) - timedelta(days=1)
    return f"{query} after:{day.strftime('%Y/%m/%d')}"


def disconnect_account() -> None:
    from vim_database.database import db, write_lock
    from vim_database.models import GmailAccount

    with write_lock:
        row = GmailAccount.query.order_by(GmailAccount.AccountID.asc()).first()
        if row is None:
            return
        row.TokenJson = None
        row.Enabled = False
        row.LastError = None
        db.session.commit()
    logger.info("[GMAIL] Disconnected mailbox")


def set_enabled(enabled: bool) -> None:
    from vim_database.database import db, write_lock
    from vim_database.models import GmailAccount

    with write_lock:
        row = GmailAccount.query.order_by(GmailAccount.AccountID.asc()).first()
        if row is None:
            return
        row.Enabled = bool(enabled)
        db.session.commit()


def save_settings(*, search_query: str | None, poll_seconds: int | None) -> None:
    from vim_database.database import db, write_lock
    from vim_database.models import GmailAccount

    with write_lock:
        row = GmailAccount.query.order_by(GmailAccount.AccountID.asc()).first()
        if row is None:
            return
        if search_query is not None:
            row.SearchQuery = search_query.strip() or default_search_query()
        if poll_seconds is not None:
            row.PollSeconds = max(30, min(int(poll_seconds), 3600))
        db.session.commit()


def is_connected() -> bool:
    row = get_account()
    return bool(row and row.TokenJson)


def _credentials_from_account(row):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from vim_database.database import db

    info = json.loads(row.TokenJson)
    creds = Credentials.from_authorized_user_info(info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        row.TokenJson = creds.to_json()
        db.session.commit()
    return creds


def _gmail_service(row):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    creds = _credentials_from_account(row)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("gmail", "v1", http=http, cache_discovery=False)


def _header(payload: dict, name: str) -> str:
    for item in (payload.get("headers") or []):
        if str(item.get("name") or "").lower() == name.lower():
            return str(item.get("value") or "").strip()
    return ""


def _walk_parts(payload: dict):
    if not payload:
        return
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk_parts(part)


def _decode_body(data: str) -> bytes:
    padded = data + "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _attachment_filename(part: dict) -> str:
    name = Path(part.get("filename") or "").name.strip()
    if name:
        return name
    mime = (part.get("mimeType") or "application/octet-stream").split(";", 1)[0].strip()
    ext = mimetypes.guess_extension(mime) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    return f"attachment{ext}"


def _usable_attachment(filename: str, data: bytes) -> bool:
    ext = Path(filename).suffix.lower()
    if ext not in extraction_config.SUPPORTED_EXTENSIONS:
        return False
    if not data:
        return False
    if ext in extraction_config.IMAGE_EXTENSIONS and len(data) < _MIN_IMAGE_BYTES:
        return False
    return True


def _download_attachments(service, message: dict) -> list[tuple[str, bytes]]:
    payload = message.get("payload") or {}
    message_id = message.get("id")
    found: list[tuple[str, bytes]] = []
    for part in _walk_parts(payload):
        filename = _attachment_filename(part)
        body = part.get("body") or {}
        data = b""
        if body.get("attachmentId"):
            att = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=body["attachmentId"])
                .execute()
            )
            data = _decode_body(att.get("data") or "")
        elif body.get("data") and part.get("filename"):
            data = _decode_body(body.get("data") or "")
        if _usable_attachment(filename, data):
            found.append((filename, data))
    return found


def _already_ingested(message_id: str) -> bool:
    from vim_database.models import GmailIngestedMessage

    return (
        GmailIngestedMessage.query.filter_by(GmailMessageId=message_id).first()
        is not None
    )


def _record_ingest(
    *,
    message_id: str,
    thread_id: str | None,
    from_address: str,
    subject: str,
    attachment_names: list[str],
    job_id: str | None,
    status: str,
    detail: str | None,
    internal_date_ms: str | None,
):
    from vim_database.database import db, write_lock
    from vim_database.models import GmailIngestedMessage

    with write_lock:
        row = GmailIngestedMessage.query.filter_by(GmailMessageId=message_id).first()
        if row is None:
            row = GmailIngestedMessage(GmailMessageId=message_id)
            db.session.add(row)
        row.ThreadId = thread_id
        row.FromAddress = (from_address or "")[:255] or None
        row.Subject = (subject or "")[:500] or None
        row.AttachmentNames = ", ".join(attachment_names)[:1000] or None
        row.JobID = job_id
        row.Status = status
        row.Detail = (detail or "")[:500] or None
        row.InternalDateMs = internal_date_ms
        db.session.commit()
    return row


def _mark_sync(error: str | None = None) -> None:
    from vim_database.database import db, write_lock

    with write_lock:
        row = get_account()
        if row is None:
            return
        row.LastSyncAt = get_ist_now()
        row.LastError = (error or "")[:500] or None
        db.session.commit()


def sync_inbox(app) -> dict:
    """
    Pull new invoice attachments from the connected mailbox and start
    the same extraction jobs used by Invoice Upload.
    """
    summary = {"checked": 0, "ingested": 0, "skipped": 0, "errors": 0, "jobs": []}
    if not _ingest_lock.acquire(blocking=False):
        logger.info("[GMAIL] Sync already running — skipping overlap")
        summary["detail"] = "A sync is already running."
        return summary

    try:
        with app.app_context():
            return _sync_inbox_locked(app, summary)
    finally:
        _ingest_lock.release()


def _sync_inbox_locked(app, summary: dict) -> dict:
    from vim.extraction import jobs
    from vim.extraction.service import stage_bytes

    row = get_account()
    if not row or not row.TokenJson:
        summary["detail"] = "Gmail is not connected."
        return summary
    if row.Enabled is False:
        summary["detail"] = "Gmail ingest is paused."
        return summary

    _mark_sync()
    try:
        service = _gmail_service(row)
        watermark = int(row.AfterInternalMs or "0")
        query = _query_since_watermark(
            (row.SearchQuery or "").strip() or default_search_query(),
            watermark,
        )
        listed = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=_MAX_MESSAGES_PER_SYNC)
            .execute()
        )
        messages = listed.get("messages") or []
        for item in messages:
            message_id = item.get("id")
            if not message_id:
                continue
            summary["checked"] += 1
            if _already_ingested(message_id):
                summary["skipped"] += 1
                continue
            meta = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
            internal_ms = int(meta.get("internalDate") or 0)
            if watermark and internal_ms and internal_ms < watermark:
                summary["skipped"] += 1
                continue
            full = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

            payload = full.get("payload") or {}
            subject = _header(payload, "Subject")
            from_address = parseaddr(_header(payload, "From"))[1] or _header(payload, "From")
            try:
                attachments = _download_attachments(service, full)
            except Exception as e:
                logger.error("[GMAIL] Attachment download failed for %s: %s", message_id, e)
                _record_ingest(
                    message_id=message_id,
                    thread_id=full.get("threadId"),
                    from_address=from_address,
                    subject=subject,
                    attachment_names=[],
                    job_id=None,
                    status="error",
                    detail=str(e)[:500],
                    internal_date_ms=str(internal_ms) if internal_ms else None,
                )
                summary["errors"] += 1
                continue

            if not attachments:
                _record_ingest(
                    message_id=message_id,
                    thread_id=full.get("threadId"),
                    from_address=from_address,
                    subject=subject,
                    attachment_names=[],
                    job_id=None,
                    status="skipped",
                    detail="No supported invoice attachments.",
                    internal_date_ms=str(internal_ms) if internal_ms else None,
                )
                summary["skipped"] += 1
                continue

            staged = []
            names = []
            try:
                for filename, data in attachments:
                    saved_path, orig_name = stage_bytes(filename, data)
                    staged.append((saved_path, orig_name))
                    names.append(orig_name)
            except Exception as e:
                logger.error("[GMAIL] Could not stage attachments for %s: %s", message_id, e)
                _record_ingest(
                    message_id=message_id,
                    thread_id=full.get("threadId"),
                    from_address=from_address,
                    subject=subject,
                    attachment_names=names,
                    job_id=None,
                    status="error",
                    detail=str(e)[:500],
                    internal_date_ms=str(internal_ms) if internal_ms else None,
                )
                summary["errors"] += 1
                continue

            job_id = jobs.create_job(staged)
            jobs.start_job(app, job_id, staged)
            _record_ingest(
                message_id=message_id,
                thread_id=full.get("threadId"),
                from_address=from_address,
                subject=subject,
                attachment_names=names,
                job_id=job_id,
                status="ingested",
                detail=None,
                internal_date_ms=str(internal_ms) if internal_ms else None,
            )
            summary["ingested"] += 1
            summary["jobs"].append(job_id)
            logger.info(
                "[GMAIL] Ingested '%s' from %s (%d file(s)) job=%s",
                subject or message_id,
                from_address,
                len(staged),
                job_id,
            )

        _mark_sync()
        return summary
    except Exception as e:
        detail = friendly_sync_error(e)
        logger.error("[GMAIL] Sync failed: %s", detail)
        try:
            _mark_sync(error=detail)
        except Exception:
            logger.exception("[GMAIL] Could not store sync error")
        summary["errors"] += 1
        summary["detail"] = detail
        return summary


def recent_messages(limit: int = 25):
    from vim_database.models import GmailIngestedMessage

    return (
        GmailIngestedMessage.query
        .order_by(GmailIngestedMessage.CreatedDate.desc())
        .limit(limit)
        .all()
    )
