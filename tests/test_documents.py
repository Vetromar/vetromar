"""Document ingestion: local parsing with structural locators, the shared
ingest_document operation (dedup by content hash, locator stamping through
the extraction hook), and the upload route. The evidence gate is untouched —
parsed text is ordinary episode raw."""

import time

import pytest

from vetromar.config import Config
from vetromar.errors import ConfigError
from vetromar.extraction import generic as generic_mod
from vetromar.extraction.generic import GenericDraft, GenericExtractionResult
from vetromar.ingest.documents import parse_document, stamp_locators
from vetromar.operations import ingest_document
from vetromar.schema import ClaimPayload, ExcerptEvidence

CFG = Config(backend="api", api_key="sk-test")
PAGE1 = "The board approved the Meridian acquisition."
PAGE2 = "Budget grows to 4M in 2027."


def _make_pdf(pages: list[str]) -> bytes:
    """Minimal text PDF (one Helvetica line per page) — enough for pypdf."""
    objects = []
    n_pages = len(pages)
    font_num = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 {font_num} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "board.pdf"
    path.write_bytes(_make_pdf([PAGE1, PAGE2]))
    return path


# -- parsing -----------------------------------------------------------------


def test_parse_pdf_pages_with_locators(pdf_path):
    parsed = parse_document(pdf_path)
    assert PAGE1 in parsed.text and PAGE2 in parsed.text
    assert [loc for _s, _e, loc in parsed.spans] == ["page:1", "page:2"]


def test_parse_txt_paragraph_locators(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("First paragraph here.\n\nSecond paragraph there.")
    parsed = parse_document(path)
    assert [loc for _s, _e, loc in parsed.spans] == ["para:1", "para:2"]


def test_parse_docx_paragraphs(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "memo.docx"
    document = docx.Document()
    document.add_paragraph("Alpha decision recorded.")
    document.add_paragraph("Beta follow-up owed.")
    document.save(str(path))
    parsed = parse_document(path)
    assert "Alpha decision recorded." in parsed.text
    assert [loc for _s, _e, loc in parsed.spans] == ["para:1", "para:2"]


def test_unsupported_and_unreadable_files_fail_friendly(tmp_path):
    with pytest.raises(ConfigError):
        parse_document(tmp_path / "slides.pptx")
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(ConfigError):
        parse_document(bad)


def test_stamp_locators_annotates_without_touching_text(pdf_path):
    parsed = parse_document(pdf_path)
    draft = GenericDraft(
        content="Budget grows",
        payload=ClaimPayload(),
        evidence=[ExcerptEvidence(text=PAGE2)],
    )
    stamp_locators([draft], parsed)
    assert draft.evidence[0].locator == "page:2"
    assert draft.evidence[0].text == PAGE2  # text is never rewritten


# -- ingest_document ----------------------------------------------------------


def _stub_model(monkeypatch):
    result = GenericExtractionResult(units=[
        GenericDraft(
            content="Meridian acquisition approved",
            payload=ClaimPayload(),
            evidence=[ExcerptEvidence(text=PAGE1)],
        )
    ])
    monkeypatch.setattr(
        generic_mod, "_call_model", lambda config, episode, text, part=None: result
    )


def test_ingest_document_lands_episode_with_locators(store, pdf_path, monkeypatch):
    _stub_model(monkeypatch)
    episode, units = ingest_document(store, CFG, pdf_path)
    assert episode.source_kind == "document"
    assert episode.title == "board"
    assert episode.external_id.startswith("file:")
    assert len(units) == 1
    assert units[0].evidence[0].locator == "page:1"
    assert units[0].provenance.method == "derived"


def test_ingest_document_rejects_same_content(store, pdf_path, monkeypatch):
    _stub_model(monkeypatch)
    ingest_document(store, CFG, pdf_path)
    with pytest.raises(ConfigError) as exc:
        ingest_document(store, CFG, pdf_path)
    assert "already in the store" in exc.value.message


def test_ingest_document_local_backend_clean_error(store, pdf_path):
    with pytest.raises(ConfigError) as exc:
        ingest_document(store, Config(backend="local"), pdf_path)
    assert "local model" in exc.value.message
    # raw layer landed; nothing derived
    assert store.list_units() == []


# -- upload route -------------------------------------------------------------


def test_upload_route_runs_document_job(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    monkeypatch.setenv("VETROMAR_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _stub_model(monkeypatch)
    from vetromar.ui_server.app import create_app

    client = TestClient(create_app())
    resp = client.post(
        "/api/documents",
        files={"file": ("board.txt", f"{PAGE1}\n\n{PAGE2}".encode(), "text/plain")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert job["result"]["units"] == 1
    assert job["result"]["title"] == "board"

    from vetromar.store import Store

    store = Store(tmp_path / "store.db")
    episode = store.get_episode(job["result"]["episode_id"])
    assert episode.source_kind == "document"
    store.close()


def test_upload_route_rejects_unsupported_suffix(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VETROMAR_DB", str(tmp_path / "store.db"))
    from vetromar.ui_server.app import create_app

    client = TestClient(create_app())
    resp = client.post(
        "/api/documents", files={"file": ("deck.pptx", b"zzzz", "application/zip")}
    )
    assert resp.status_code == 400
