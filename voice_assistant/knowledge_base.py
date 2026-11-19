"""VisionSync local document knowledge v1 (SQLite FTS5, no cloud service).
Index files placed in knowledge_docs. Supports .txt/.md by default, optional
.pdf via pypdf and .docx via python-docx. Text-based PDF only.
"""
import argparse
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "knowledge_docs"
INDEX_DB = ROOT / "vision_knowledge.db"
MAX_BYTES = 15 * 1024 * 1024
SUFFIXES = {".txt", ".md", ".pdf", ".docx"}
STOP = set("what where when which who why how do does did can could would should is are the a an for in of to from on about my me i you please tell say explain summarize summary according document documents pdf file files this that and or with".split())

def connection():
    c = sqlite3.connect(str(INDEX_DB), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c

def initialize():
    DOCS_DIR.mkdir(exist_ok=True)
    with connection() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            digest TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        )""")
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            document_id UNINDEXED, filename UNINDEXED,
            chunk_number UNINDEXED, content, tokenize='unicode61'
        )""")

def extract_text(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("File exceeds 15 MB limit")
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("For PDF indexing install: python -m pip install pypdf") from e
        reader = PdfReader(str(path))
        if len(reader.pages) > 300:
            raise ValueError("PDF has more than 300 pages")
        return "\n".join(
            f"[Page {n}]\n" + (page.extract_text() or "")
            for n, page in enumerate(reader.pages, 1)
        )
    if path.suffix.lower() == ".docx":
        try:
            from docx import Document
        except ImportError as e:
            raise RuntimeError("For DOCX indexing install: python -m pip install python-docx") from e
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    raise ValueError("Unsupported file extension")

def chunks_of(text, limit=1100, overlap=130):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + limit, len(text))
        if end < len(text):
            gap = text.rfind(" ", pos + limit // 2, end)
            if gap > pos: end = gap
        body = text[pos:end].strip()
        if body: chunks.append(body)
        if end >= len(text): break
        pos = max(pos+1, end - overlap)
    return chunks

def index_documents():
    """Reindex changed documents, delete index entries for removed documents."""
    initialize()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "removed": 0, "errors": []}
    files = sorted(p for p in DOCS_DIR.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES)
    current = {p.relative_to(DOCS_DIR).as_posix() for p in files}
    with connection() as c:
        existing = {row["path"]: dict(row) for row in c.execute("SELECT * FROM documents")}
        for stale in existing.keys() - current:
            doc_id = existing[stale]["id"]
            c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            stats["removed"] += 1

        for path in files:
            name = path.relative_to(DOCS_DIR).as_posix()
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if name in existing and existing[name]["digest"] == digest:
                    stats["unchanged"] += 1
                    continue
                body = extract_text(path)
                parts = chunks_of(body)
                if not parts:
                    raise ValueError("No extractable text (scanned PDFs require OCR)")
                if name in existing:
                    ident = existing[name]["id"]
                    c.execute("DELETE FROM chunks WHERE document_id=?", (ident,))
                    c.execute("UPDATE documents SET digest=?, indexed_at=? WHERE id=?",
                              (digest, datetime.now(timezone.utc).isoformat(), ident))
                    stats["updated"] += 1
                else:
                    cursor = c.execute("INSERT INTO documents(path,digest,indexed_at) VALUES (?,?,?)",
                        (name, digest, datetime.now(timezone.utc).isoformat()))
                    ident = cursor.lastrowid
                    stats["new"] += 1
                c.executemany(
                    "INSERT INTO chunks(document_id,filename,chunk_number,content) VALUES(?,?,?,?)",
                    [(ident,name,i+1,part) for i,part in enumerate(parts)]
                )
            except Exception as exc:
                stats["errors"].append(f"{name}: {exc}")
        return stats

def search_documents(question, limit=4):
    """Search local text excerpts. Empty results means insufficient indexed evidence."""
    initialize()
    words = re.findall(r"[a-zA-Z0-9]+", question.casefold())
    meaningful = []
    for word in words:
        if len(word) > 2 and word not in STOP and word not in meaningful:
            meaningful.append(word)
    if not meaningful:
        return []
    expression = " OR ".join(f'"{w}"' for w in meaningful[:12])
    with connection() as c:
        rows = c.execute("""
            SELECT filename, chunk_number, content, bm25(chunks) AS rank
            FROM chunks WHERE chunks MATCH ?
            ORDER BY rank LIMIT ?
        """, (expression, min(max(int(limit)*3,4),24))).fetchall()
    results = []
    for row in rows:
        # At least one non-stopword hit; match can be partial topical evidence.
        hits = sum(bool(re.search(r"\b"+re.escape(w)+r"\b",row["content"],re.I)) for w in meaningful)
        if not hits: continue
        results.append({
            "filename":row["filename"], "chunk":row["chunk_number"],
            "excerpt":row["content"], "hits":hits,
        })
    results.sort(key=lambda r: r["hits"],reverse=True)
    return results[:limit]

def local_context(question, limit=4):
    rows = search_documents(question,limit)
    if not rows:
        return ""
    pieces = [f"[Source: {row['filename']} | excerpt {row['chunk']}]\n{row['excerpt']}" for row in rows]
    return "\n\n".join(pieces)

def status():
    initialize()
    with connection() as c:
        documents = c.execute("SELECT count(*) FROM documents").fetchone()[0]
        chunks = c.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return {"documents":documents,"chunks":chunks,"folder":str(DOCS_DIR)}

if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Local document retrieval for VisionSync")
    cli.add_argument("command", choices=["index","status","search"])
    cli.add_argument("query", nargs="?")
    args = cli.parse_args()
    if args.command == "index":
        print(index_documents())
    elif args.command == "status":
        print(status())
    else:
        if not args.query:
            cli.error("search requires a question")
        for row in search_documents(args.query):
            print(f"\n{row['filename']} [excerpt {row['chunk']}]: {row['excerpt'][:500]}")
