"""Brand profile per project — scopes every external finding to THIS brand."""
from __future__ import annotations

import json

from ..db import now_iso
from ..util import registrable_domain

_DEFAULT_NOTES = ("'SDS' alone is a generic industry term (safety data sheet) — a mention "
                  "counts only if it clearly refers to our product, SDS Manager.")


def _j(v):
    try:
        return json.loads(v) if v else []
    except (json.JSONDecodeError, TypeError):
        return []


def ensure_brand_profile(conn, source_id: int) -> dict:
    row = conn.execute("SELECT * FROM brand_profiles WHERE source_id=?", (source_id,)).fetchone()
    if row:
        return get_brand(conn, source_id)
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    dom = registrable_domain(src["location"]) if src and src["location"] else None
    brand = "SDS Manager"
    conn.execute(
        """INSERT INTO brand_profiles (source_id, brand_name, aliases, own_domains,
             disambiguation_notes, negative_terms, updated_at) VALUES (?,?,?,?,?,?,?)""",
        (source_id, brand, json.dumps(["SDSManager", "SDS Manager app", dom or "sdsmanager.com"]),
         json.dumps([d for d in [dom, "sdsmanager.com", "sdsmanager.no", "sdsmanager.es"] if d]),
         _DEFAULT_NOTES, json.dumps([]), now_iso()),
    )
    conn.commit()
    return get_brand(conn, source_id)


def get_brand(conn, source_id: int) -> dict:
    r = conn.execute("SELECT * FROM brand_profiles WHERE source_id=?", (source_id,)).fetchone()
    if not r:
        return {}
    return {"source_id": source_id, "brand_name": r["brand_name"], "aliases": _j(r["aliases"]),
            "own_domains": _j(r["own_domains"]), "disambiguation_notes": r["disambiguation_notes"],
            "negative_terms": _j(r["negative_terms"])}


def set_brand(conn, source_id, brand_name, aliases, own_domains, notes, negative_terms) -> None:
    conn.execute(
        """INSERT INTO brand_profiles (source_id, brand_name, aliases, own_domains,
             disambiguation_notes, negative_terms, updated_at) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET brand_name=excluded.brand_name,
             aliases=excluded.aliases, own_domains=excluded.own_domains,
             disambiguation_notes=excluded.disambiguation_notes,
             negative_terms=excluded.negative_terms, updated_at=excluded.updated_at""",
        (source_id, brand_name, json.dumps(aliases), json.dumps(own_domains), notes,
         json.dumps(negative_terms), now_iso()),
    )
    conn.commit()
