"""Typer CLI entry point for the Crawling & Analyzing System."""
from __future__ import annotations

import asyncio

import typer

from .db import connect, resolve_source

app = typer.Typer(help="Internal QA crawler/analyzer for sdsmanager.com", no_args_is_help=True)
source_app = typer.Typer(help="Manage watched sources", no_args_is_help=True)
analyze_app = typer.Typer(help="Analysis passes", no_args_is_help=True)
inventory_app = typer.Typer(help="Site-wide claim inventory", no_args_is_help=True)
report_app = typer.Typer(help="Reports", no_args_is_help=True)
issues_app = typer.Typer(help="Inspect / update issues", no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(analyze_app, name="analyze")
app.add_typer(inventory_app, name="inventory")
app.add_typer(report_app, name="report")
app.add_typer(issues_app, name="issues")


@app.callback()
def _global(
    fast_model: str = typer.Option(None, "--fast-model", help="Override the LLM fast/screening model (OpenRouter id)"),
    reasoning_model: str = typer.Option(None, "--reasoning-model", help="Override the LLM reasoning/verify model"),
    model: str = typer.Option(None, "--model", help="Override BOTH tiers with one model id"),
):
    """Model-choice flags apply to any command run this invocation."""
    import os

    if model:
        os.environ["SDS_FAST_MODEL"] = model
        os.environ["SDS_REASONING_MODEL"] = model
    if fast_model:
        os.environ["SDS_FAST_MODEL"] = fast_model
    if reasoning_model:
        os.environ["SDS_REASONING_MODEL"] = reasoning_model


@app.command("migrate")
def migrate_cmd(no_backup: bool = typer.Option(False, "--no-backup")):
    """Migrate the DB to the fact-checking model (idempotent; backs up first)."""
    from .migrate import migrate

    migrate(backup=not no_backup)


@app.command("models")
def models_show():
    """Show the effective LLM models (after env/flag overrides)."""
    import os
    from .config import settings as _s

    s = _s()["llm"]
    fast = os.getenv("SDS_FAST_MODEL") or s["fast_model"]
    reasoning = os.getenv("SDS_REASONING_MODEL") or s["reasoning_model"]
    typer.echo(f"fast_model      : {fast}")
    typer.echo(f"reasoning_model : {reasoning}")
    typer.echo("Override with: --fast-model / --reasoning-model / --model  (or SDS_FAST_MODEL / SDS_REASONING_MODEL)")


def _source_or_exit(conn, ref: str):
    src = resolve_source(conn, ref)
    if not src:
        typer.secho(f"No source matching '{ref}'. Use `source list`.", fg="red")
        raise typer.Exit(1)
    return src


# ---- source ----
@source_app.command("add")
def source_add(ref: str, name: str = typer.Option(None, "--name", help="label")):
    """Add a sitemap URL, website root URL, or a .txt/.csv URL-list file."""
    from .ingest import add_and_ingest

    add_and_ingest(connect(), ref, name)


@source_app.command("list")
def source_list():
    from .db import list_sources

    conn = connect()
    rows = list_sources(conn)
    if not rows:
        typer.echo("No sources yet. Add one with `source add <url|file>`.")
        return
    for s in rows:
        n = conn.execute("SELECT COUNT(*) c FROM urls WHERE source_id=?", (s["id"],)).fetchone()["c"]
        typer.echo(f"#{s['id']}  {s['kind']:<8}  urls={n:<5}  {s['name'] or ''}  {s['location']}")


# ---- crawl ----
@app.command()
def crawl(
    source: str,
    only_changed: bool = typer.Option(False, "--only-changed"),
    limit: int = typer.Option(None, "--limit"),
    concurrency: int = typer.Option(None, "--concurrency"),
):
    """Crawl a source's URLs; record status + extracted content + FAQs."""
    from .crawler import crawl_source

    conn = connect()
    src = _source_or_exit(conn, source)
    asyncio.run(crawl_source(conn, src["id"], only_changed=only_changed, limit=limit, concurrency=concurrency))


# ---- analyze ----
@analyze_app.command("technical")
def analyze_technical(source: str):
    from .analysis.technical import analyze_technical as run

    conn = connect()
    src = _source_or_exit(conn, source)
    run(conn, src["id"])


@analyze_app.command("facts")
def analyze_facts(source: str, all_locales: bool = typer.Option(False, "--all-locales")):
    from .analysis.facts import analyze_facts_regex
    from .analysis.inventory import consistency_check
    from .analysis.fact_check import fact_check_llm

    conn = connect()
    src = _source_or_exit(conn, source)
    analyze_facts_regex(conn, src["id"])
    consistency_check(conn, src["id"])
    asyncio.run(fact_check_llm(conn, src["id"], all_locales=all_locales))


@analyze_app.command("features")
def analyze_features(source: str, all_locales: bool = typer.Option(False, "--all-locales")):
    from .analysis.features import analyze_features_llm

    conn = connect()
    src = _source_or_exit(conn, source)
    asyncio.run(analyze_features_llm(conn, src["id"], all_locales=all_locales))


@analyze_app.command("faqs")
def analyze_faqs(source: str, all_locales: bool = typer.Option(False, "--all-locales")):
    from .analysis.faqs import analyze_faqs as run

    conn = connect()
    src = _source_or_exit(conn, source)
    asyncio.run(run(conn, src["id"], all_locales=all_locales))


@analyze_app.command("cannibalization")
def analyze_cannibalization(source: str):
    from .analysis.cannibalization import analyze_cannibalization as run

    conn = connect()
    src = _source_or_exit(conn, source)
    asyncio.run(run(conn, src["id"]))


# ---- inventory ----
@inventory_app.command("claims")
def inventory_claims(source: str):
    from .analysis.inventory import export_claims

    conn = connect()
    src = _source_or_exit(conn, source)
    export_claims(conn, src["id"])


# ---- report ----
@report_app.command("html")
def report_html(source: str):
    from .report.html_report import generate_html

    conn = connect()
    src = _source_or_exit(conn, source)
    generate_html(conn, src["id"])


@report_app.command("csv")
def report_csv(source: str):
    from .report.csv_export import export_csv

    conn = connect()
    src = _source_or_exit(conn, source)
    export_csv(conn, src["id"])


# ---- run-all ----
@app.command("run-all")
def run_all(
    source: str,
    only_changed: bool = typer.Option(False, "--only-changed"),
    limit: int = typer.Option(None, "--limit"),
    concurrency: int = typer.Option(None, "--concurrency"),
    all_locales: bool = typer.Option(False, "--all-locales"),
    no_llm: bool = typer.Option(False, "--no-llm"),
):
    """Full pipeline for a source with run diffing."""
    from .crawler import crawl_source
    from .analysis.technical import analyze_technical
    from .analysis.facts import analyze_facts_regex
    from .analysis.inventory import consistency_check, export_claims
    from .analysis.fact_check import fact_check_llm
    from .analysis.features import analyze_features_llm
    from .analysis.faqs import analyze_faqs
    from .analysis.cannibalization import analyze_cannibalization
    from .report.html_report import generate_html
    from .report.csv_export import export_csv
    from .db import reconcile_fixed, start_run, finish_run, now_iso, CATEGORIES

    conn = connect()
    src = _source_or_exit(conn, source)
    sid = src["id"]
    run_id, run_start = start_run(conn, sid, "run-all")
    print(f"=== run-all source #{sid} started {run_start} ===")

    asyncio.run(crawl_source(conn, sid, only_changed=only_changed, limit=limit, concurrency=concurrency))
    analyze_technical(conn, sid)
    analyze_facts_regex(conn, sid)
    consistency_check(conn, sid)
    export_claims(conn, sid)
    if not no_llm:
        asyncio.run(fact_check_llm(conn, sid, all_locales=all_locales))
        asyncio.run(analyze_features_llm(conn, sid, all_locales=all_locales))
        asyncio.run(analyze_faqs(conn, sid, all_locales=all_locales))
        asyncio.run(analyze_cannibalization(conn, sid))

    fixed = reconcile_fixed(conn, sid, CATEGORIES, run_start)
    if fixed:
        print(f"Diff: auto-marked {fixed} previously-open issues as fixed.")
    export_csv(conn, sid)
    generate_html(conn, sid)
    finish_run(conn, run_id)
    print("=== run-all complete ===")


# ---- issues ----
@issues_app.command("list")
def issues_list(
    category: str = typer.Option(None, "--category"),
    severity: str = typer.Option(None, "--severity"),
    status: str = typer.Option("open", "--status"),
    limit: int = typer.Option(100, "--limit"),
):
    conn = connect()
    where, params = [], []
    if category:
        where.append("i.category=?"); params.append(category)
    if severity:
        where.append("i.severity=?"); params.append(severity)
    if status:
        where.append("i.status=?"); params.append(status)
    sql = (
        "SELECT i.id,i.severity,i.category,i.title,u.url FROM issues i "
        "JOIN urls u ON u.id=i.url_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        typer.echo("No matching issues.")
        return
    for r in rows:
        typer.echo(f"#{r['id']:<5} {r['severity']:<8} {r['category']:<15} {r['title'][:36]:<36} {r['url']}")
    typer.echo(f"\n{len(rows)} issue(s).")


@issues_app.command("mark")
def issues_mark(id: int, status: str):
    if status not in ("open", "fixed", "ignored"):
        typer.secho("status must be open|fixed|ignored", fg="red")
        raise typer.Exit(1)
    conn = connect()
    cur = conn.execute("UPDATE issues SET status=? WHERE id=?", (status, id))
    conn.commit()
    typer.echo(f"Issue #{id} -> {status}" if cur.rowcount else f"No issue #{id}.")


def main():
    app()


if __name__ == "__main__":
    main()
