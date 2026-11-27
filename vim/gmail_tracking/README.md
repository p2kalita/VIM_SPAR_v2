# Gmail ingest outcome tracking

Shows what happened to every attachment that Gmail ingest sent to
extraction: whether it became an invoice, is waiting on a decision in
Rejected Documents, was not an invoice, or failed.

Admin page: **`/admin/ingest-outcomes`** (sidebar: *Ingest outcomes*).
Access: the same admin permission as the other admin pages (`user.manage`).

The module only reads `gmail_ingested_message`, `invoice_document`,
`invoice`, `rejected_document`, and the in-memory extraction jobs. It writes
only its own two tables. It never changes the Gmail mailbox; the OAuth scope
stays `gmail.readonly`.

## States

| Attachment state | Meaning |
|---|---|
| Processed | An invoice exists in VIM for this file (links to the invoice). |
| Pending | Held in Rejected Documents waiting on a decision: not an invoice (PO, quote, bill of supply), vendor not registered, or incomplete header. Also used for a document that was proceeded but has no invoice yet. Links to the rejected document. |
| Not invoice | Auto-rejected as empty or irrelevant. No one needs to act. |
| Dismissed | An admin stopped the held document. |
| Error | Extraction or save failed, or the linked invoice was later deleted. |
| In progress | The extraction job has not finished yet. |

An **email's status** is the first of these that any of its attachments is in:
Error → Pending → In progress → Processed → Dismissed → Not invoice.

**Sent to extraction** means the outcome could not be linked. This covers
emails ingested before this module existed, emails older than the lookback
window, and emails whose job was lost and whose files could not be matched
unambiguously.

State is worked out each time the page loads, so when an admin proceeds a
pending document and it becomes an invoice, the email shows Processed on the
next load.

## How outcomes are linked

1. **From the job (exact).** Every `GMAIL_TRACKING_SECONDS` a background
   thread (`gmail-tracking`) looks at ingested emails and reads their
   finished extraction job. It records each file's stored name, status,
   failure detail, and invoice id.
2. **Fallback (best effort).** Jobs live in memory for one hour and are lost
   on restart. If the job is gone, the attachment's file name is matched
   against `invoice_document.FileName` / `rejected_document.FileName` created
   between 2 minutes before and `GMAIL_TRACKING_MATCH_HOURS` after the email
   was ingested. A link is made only when there is **exactly one** match not
   already claimed by another email. After 3 attempts with no match, the
   email is marked unlinkable.

Fallback limits, by design:
- Errors (extraction/database failures) are not saved anywhere the fallback
  can see, so they cannot be recovered once the job is gone.
- When the same file name appears more than once in the window (or twice in
  one email), nothing is linked rather than risk linking the wrong file.
- Attachment names beyond the 1000-character `AttachmentNames` limit cannot
  be fallback-matched.

## Settings (`.env`, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `GMAIL_TRACKING` | `1` | `0` / `false` / `off` / `no` stops the background reconciler. The page still works for what is already linked. Independent of `GMAIL_POLLING`. |
| `GMAIL_TRACKING_SECONDS` | `30` | Reconciler interval, clamped to 10–600. |
| `GMAIL_TRACKING_LOOKBACK_DAYS` | `7` | How far back emails are reconciled (1–365). Older unlinked emails show *Sent to extraction*. |
| `GMAIL_TRACKING_MATCH_HOURS` | `2` | Fallback match window after ingest (1–72). |

## Files

```
vim/gmail_tracking/
  __init__.py     init_gmail_tracking(app): tables, blueprint, reconciler
  models.py       gmail_ingested_attachment, gmail_ingest_reconcile
  settings.py     the environment variables above
  reconciler.py   background linking (job capture + fallback)
  outcomes.py     state derivation, rollup, dashboard query
  routes.py       /admin/ingest-outcomes
templates/vim_admin_gmail_tracking.html
tests/gmail_tracking/   isolated tests (temporary SQLite, never the real DB)
```

Run the tests:

```
venv\python.exe -m pip install -r requirements-dev.txt
venv\python.exe -m pytest tests/gmail_tracking -q
```

## Wiring and rollback

Two edits outside this folder connect the module:

1. `app.py`, right after `register_routes(app)` (it must stay before
   `db.create_all()`):
   ```python
   from vim.gmail_tracking import init_gmail_tracking
   init_gmail_tracking(app)
   ```
2. `templates/admin_base.html`: the *Ingest outcomes* nav item.

To roll back, remove both. The two tables become inert and can be left or
dropped. Nothing else references them.
