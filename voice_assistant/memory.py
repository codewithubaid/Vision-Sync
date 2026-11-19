"""VisionSync Memory Engine v1.0.
Keeps the existing `memories` table intact and adds `memory_items` for
explicit, editable, local memories. No cloud calls are made by this module.
"""
import sqlite3
import re
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
DATABASE = str(BASE_DIR / "vision_memory.db")

def _conn():
    c = sqlite3.connect(DATABASE, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c

def create_database():
    with _conn() as c:
        # Backwards compatible with the user's original memory.py
        c.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT, information TEXT, timestamp TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE COLLATE NOCASE,
            information TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'personal',
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _key(value):
    v = re.sub(r"\s+", " ", str(value).strip().lower())
    v = re.sub(r"[^a-z0-9 ]", "", v)
    return v[:120]

def save_memory(user, information):
    """Legacy API retained. No change to existing callers."""
    info = str(information or "").strip()
    if not info:
        raise ValueError("Memory text cannot be blank.")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO memories(user,information,timestamp) VALUES(?,?,?)",
            (str(user), info, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        return cur.lastrowid

def _normalize_profile_memory(information, category, key):
    """Normalize only well-identified profile facts, never guess from a bare name.

    A plain value such as 'Teena' without a relationship key/category is *not*
    enough evidence to infer whose name it is.
    """
    info = re.sub(r"\s+", " ", str(information or "")).strip().rstrip(".")
    raw_key = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    raw_category = re.sub(r"[^a-z0-9]", "", str(category or "").lower())
    aliases = {
        "profilemothername": "profilemothername", "mothername": "profilemothername",
        "mothersname": "profilemothername", "momname": "profilemothername",
        "momsname": "profilemothername", "mumname": "profilemothername",
        "profilefathername": "profilefathername", "fathername": "profilefathername",
        "fathersname": "profilefathername", "dadname": "profilefathername",
        "dadsname": "profilefathername",
        "profileusername": "profileusername", "username": "profileusername",
        "profilemothername": "profilemothername", "profilefathername": "profilefathername",
        "myname": "profileusername", "usersname": "profileusername",
    }
    # Do not infer the fact from the *value*. Match relationships only.
    target = aliases.get(raw_key) or aliases.get(raw_category)
    patterns = (
        ("profilemothername", r"^(?:(?:remember|please remember)(?: that)?\s+)?(?:my|your)\s+(?:mother(?:'s|s)?|mom(?:'s|s)?|mum(?:'s|s)?)\s+name\s+is\s+(.+)$"),
        ("profilefathername", r"^(?:(?:remember|please remember)(?: that)?\s+)?(?:my|your)\s+(?:father(?:'s|s)?|dad(?:'s|s)?)\s+name\s+is\s+(.+)$"),
        ("profileusername", r"^(?:(?:remember|please remember)(?: that)?\s+)?(?:my|your)\s+name\s+is\s+(.+)$"),
    )
    match_target = None
    for slot, pattern in patterns:
        found = re.match(pattern, info, re.I)
        if found:
            match_target = slot
            info = found.group(1).strip(" .,!?:;")
            break
    # Reject contradictory metadata, rather than misclassify a relationship.
    if target and match_target and target != match_target:
        raise ValueError("Conflicting profile relationship in memory fields.")
    target = target or match_target
    if target:
        info = info.strip(" .,!?:;")
        if not info or len(info) > 150:
            raise ValueError("Profile value must contain 1 to 150 characters.")
        return info, "profile", target
    return info, category, key


def remember(information, category="personal", key=None):
    """Save an explicit fact; exact duplicate or key overwrites instead of duplicating."""
    info, category, key = _normalize_profile_memory(information, category, key)
    if not info or len(info) > 1000:
        raise ValueError("Memory must contain 1 to 1000 characters.")
    identity = _key(key or info)
    if not identity:
        raise ValueError("Memory key cannot be blank.")
    stamp = _now()
    with _conn() as c:
        c.execute("""
            INSERT INTO memory_items(key, information, category, updated_at, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                information=excluded.information,
                category=excluded.category,
                updated_at=excluded.updated_at
        """, (identity, info, str(category), stamp, stamp))
        row = c.execute("SELECT id FROM memory_items WHERE key = ?", (identity,)).fetchone()
        return row["id"]

def list_memories(limit=50):
    with _conn() as c:
        rows = c.execute("""
            SELECT id,key,information,category,updated_at
            FROM memory_items ORDER BY updated_at DESC,id DESC LIMIT ?
        """, (min(max(int(limit),1),200),)).fetchall()
        return [dict(row) for row in rows]

def search_memories(query, limit=8):
    """
    Retrieve relevant memories with lightweight lexical ranking.

    Searches both the editable memory_items table and the legacy memories
    table. This keeps retrieval local and requires no additional packages.
    """
    stopwords = {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "can", "could", "did", "do", "does", "for", "from", "had", "has",
        "have", "how", "i", "if", "in", "into", "is", "it", "me", "my",
        "of", "on", "or", "our", "please", "should", "so", "that", "the",
        "their", "them", "there", "these", "they", "this", "to", "us",
        "was", "we", "what", "when", "where", "which", "who", "why",
        "will", "with", "would", "you", "your", "user", "assistant"
    }

    def tokens(value):
        result = set()
        for word in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if len(word) <= 1 or word in stopwords:
                continue
            # Small, dependency-free normalisation for common English endings.
            if len(word) > 5 and word.endswith("ing"):
                word = word[:-3]
            elif len(word) > 4 and word.endswith("ed"):
                word = word[:-2]
            elif len(word) > 4 and word.endswith("ies"):
                word = word[:-3] + "y"
            elif len(word) > 4 and word.endswith("s"):
                word = word[:-1]
            result.add(word)
        return result

    query_text = re.sub(r"\s+", " ", str(query or "")).strip().lower()
    query_terms = tokens(query_text)
    if not query_terms:
        return []

    try:
        max_results = min(max(int(limit), 1), 20)
    except (TypeError, ValueError):
        max_results = 8

    candidates = list_memories(200)

    # Keep older records searchable without altering the legacy table.
    with _conn() as c:
        legacy_rows = c.execute("""
            SELECT id, information, timestamp
            FROM memories
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

    for row in legacy_rows:
        info = str(row["information"] or "").strip()
        if info:
            candidates.append({
                "id": row["id"],
                "key": "",
                "information": info,
                "category": "legacy",
                "updated_at": row["timestamp"] or "",
                "legacy": True,
            })

    ranked = []
    for row in candidates:
        info = str(row.get("information", ""))
        searchable = (
            f"{info} {row.get('key', '')} {row.get('category', '')}"
        ).lower()
        overlap = query_terms & tokens(searchable)
        if not overlap:
            continue

        coverage = len(overlap) / max(len(query_terms), 1)
        score = (len(overlap) * 2.0) + coverage
        normalized_info = re.sub(r"\s+", " ", info.lower())
        if len(query_text) >= 5 and query_text in normalized_info:
            score += 4.0

        # Avoid weak single-word matches for longer questions.
        minimum_overlap = 1 if len(query_terms) <= 4 else 2
        if len(overlap) < minimum_overlap and coverage < 0.5:
            continue

        result = dict(row)
        result["relevance"] = round(score, 3)
        ranked.append(result)

    ranked.sort(
        key=lambda item: (
            item["relevance"],
            str(item.get("updated_at", "")),
            int(item.get("id", 0))
        ),
        reverse=True
    )
    return ranked[:max_results]


# Named relationships are stored under stable keys. A corrected value replaces
# only its own slot, keeping parents' names separate from the user's name.
PERSONAL_FACT_LABELS = {
    "profile.user_name": "Your name",
    "profile.father_name": "Your father's name",
    "profile.mother_name": "Your mother's name",
    "profile.favorite_ice_cream": "Your favorite ice cream flavor",
}

def set_personal_fact(slot, value):
    if slot not in PERSONAL_FACT_LABELS:
        raise ValueError("Unknown personal fact slot")
    value = re.sub(r"\s+", " ", str(value or "")).strip(" .,!?:;")
    if not value or len(value) > 120:
        raise ValueError("Personal fact must be 1 to 120 characters")
    return remember(value, category="profile", key=slot)

def get_personal_fact(slot):
    if slot not in PERSONAL_FACT_LABELS:
        return None
    with _conn() as c:
        row = c.execute("SELECT information FROM memory_items WHERE key=?", (_key(slot),)).fetchone()
    if row is None:
        return None
    prefix = PERSONAL_FACT_LABELS[slot] + " is "
    text = row["information"]
    return text[len(prefix):].rstrip(".") if text.startswith(prefix) else text

def personal_facts():
    return {slot: value for slot in PERSONAL_FACT_LABELS
            if (value := get_personal_fact(slot)) is not None}

def forget_memory(memory_id):
    with _conn() as c:
        cur = c.execute("DELETE FROM memory_items WHERE id=?", (int(memory_id),))
        return cur.rowcount > 0

def forget_matching(query):
    """Delete only when there is one unambiguous exact/substring match."""
    q = _key(query)
    if not q:
        return ("missing", None)
    rows = list_memories(200)
    matches = [x for x in rows if q == _key(x["information"]) or q == x["key"]]
    if not matches:
        matches = [x for x in rows if q in _key(x["information"])]
    if len(matches) != 1:
        return ("ambiguous" if matches else "missing", matches)
    match = matches[0]
    forget_memory(match["id"])
    return ("deleted", match)


def forget_memory_by_text(description):
    """Delete a memory by its content/key, without exposing database IDs."""
    status, result = forget_matching(description)
    if status == "deleted":
        return {"success": True, "deleted": True, "information": result["information"]}
    if status == "ambiguous":
        return {"success": False, "error": "More than one memory matches. Please use a more specific description."}
    return {"success": False, "error": "No matching memory was found."}


def get_memory():
    """Legacy-facing string. Reads old and new tables; stays bounded."""
    with _conn() as c:
        old = c.execute(
            "SELECT information FROM memories ORDER BY id DESC LIMIT 8"
        ).fetchall()
    new = list_memories(20)
    text = [x["information"] for x in new]
    for row in old:
        if row["information"] not in text:
            text.append(row["information"])
    return "\n".join(text[:25])

create_database()
