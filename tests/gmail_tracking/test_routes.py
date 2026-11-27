import factories as f

URL = "/admin/ingest-outcomes"


def test_anonymous_redirected_to_login(anon_client):
    resp = anon_client.get(URL)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_non_admin_denied_like_other_admin_pages(user_client):
    ours = user_client.get(URL)
    theirs = user_client.get("/admin/rejected_documents")
    assert ours.status_code == theirs.status_code == 302
    assert ours.headers["Location"] == theirs.headers["Location"]


def test_admin_gets_page(admin_client):
    resp = admin_client.get(URL)
    assert resp.status_code == 200
    assert b"Gmail ingest outcomes" in resp.data


def _seed(app):
    with app.app_context():
        pending = f.message(names=("po.pdf",))
        po = f.attachment(pending, original="po.pdf", status="not_invoice")
        rej = f.rejection(stored_name=po.StoredFileName, decision="pending")

        done = f.message(names=("a.pdf",))
        a = f.attachment(done, original="a.pdf", status="success")
        inv = f.invoice(storage_path=a.StoragePath)
        return pending.IngestID, done.IngestID, rej.RejectionID, inv.InvoiceID


def test_pending_filter_lists_only_pending(app, admin_client):
    pending_id, done_id, *_ = _seed(app)
    html = admin_client.get(URL + "?status=pending").get_data(as_text=True)
    assert f'data-ingest-id="{pending_id}"' in html
    assert f'data-ingest-id="{done_id}"' not in html


def test_links_to_invoice_and_rejected_document(app, admin_client):
    pending_id, done_id, rejection_id, invoice_id = _seed(app)
    html = admin_client.get(URL).get_data(as_text=True)
    assert f'href="/admin/invoice/{invoice_id}/view"' in html
    assert f'href="/admin/rejected_documents/{rejection_id}"' in html


def test_unknown_filter_and_bad_page_fall_back(app, admin_client):
    _seed(app)
    assert admin_client.get(URL + "?status=bogus&page=abc").status_code == 200


def test_pagination(app, admin_client, monkeypatch):
    from vim.gmail_tracking import routes

    monkeypatch.setattr(routes, "PER_PAGE", 2)
    with app.app_context():
        ids = [f.message().IngestID for _ in range(5)]
    first = admin_client.get(URL).get_data(as_text=True)
    third = admin_client.get(URL + "?page=3").get_data(as_text=True)
    assert "Page 1 of 3" in first
    assert sum(f'data-ingest-id="{i}"' in first for i in ids) == 2
    assert sum(f'data-ingest-id="{i}"' in third for i in ids) == 1


def test_init_twice_registers_once(app):
    from vim.gmail_tracking import init_gmail_tracking

    init_gmail_tracking(app)
    init_gmail_tracking(app)
    rules = [r.rule for r in app.url_map.iter_rules() if r.endpoint == "gmail_tracking.dashboard"]
    assert rules == [URL]


def _active_nav(html):
    import re

    return [
        re.sub(r"<[^>]+>|\s+", " ", m).strip()
        for m in re.findall(r'<a class="nav-item active"[^>]*>(.*?)</a>', html, re.S)
    ]


def test_nav_highlight_on_outcomes_page(admin_client):
    html = admin_client.get(URL).get_data(as_text=True)
    assert _active_nav(html) == ["Ingest outcomes"]


def test_nav_highlight_on_gmail_page(admin_client):
    resp = admin_client.get("/admin/gmail")
    assert resp.status_code == 200
    assert _active_nav(resp.get_data(as_text=True)) == ["Gmail ingest"]
