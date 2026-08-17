"""FastAPI dashboard.

Pages are rendered server-side; /api/* mirrors each view as JSON for reports and any future
consumer. Filters travel as query params, so every view is linkable and bookmarkable.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (auth, bundle, config, db, errors, exports, identity, ingest, metrics, registry,
               rollup, scheduler, sources, startup)

from . import __version__, build_info      # noqa: E402  (kept beside the app metadata)

# Templates and static files ship inside the bundle in a packaged build, so they are located
# the same way schema.sql is rather than relative to this module.
WEB = config.resource("ota_analytics", "web")
BUILD = build_info()
app = FastAPI(title="InTouch OTA Analytics", version=__version__)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB / "templates"))


def get_conn() -> sqlite3.Connection:
    return db.connect()


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Default-deny. Every route needs a session unless it is explicitly public.

    Enforced here rather than per route on purpose: a route added later is protected by
    omission. The alternative — remembering a dependency on each of ~30 handlers — fails the
    first time someone forgets, and the thing left open would be a page listing IMEIs and VINs.
    """
    path = request.url.path

    # With no password configured there is nobody who *can* log in. Refusing everything would
    # brick a local install on upgrade, so the app stays open and says so loudly on every page;
    # the deployment runbook sets the hashes before it is ever reachable from outside.
    if not auth.is_configured() or auth.is_public(path):
        return await call_next(request)

    user = auth.identify(request)
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    # A viewer reads. Blocking unsafe methods here is what actually enforces it — hiding the
    # buttons in a template is a courtesy, not a permission.
    if request.method not in auth.SAFE_METHODS and not user.is_admin:
        if path.startswith("/api/"):
            return JSONResponse({"error": "read-only account"}, status_code=403)
        return HTMLResponse(
            '<!doctype html><html><head><title>Read-only</title>'
            '<link rel="stylesheet" href="/static/app.css"></head><body><main>'
            '<h1>Read-only account</h1><p>This account can view the dashboard but not change '
            'anything.</p><p><a href="/">Back to the dashboard</a></p>'
            '</main></body></html>', status_code=403)

    request.state.user = user
    return await call_next(request)


# Identifies a running copy as *this* application, so a second launch can tell the difference
# between our own dashboard already serving and some unrelated thing holding the port.
APP_ID = "intouch-ota-analytics"


@app.get("/healthz")
def healthz():
    """Liveness for the service manager and the tunnel. Deliberately says nothing about data."""
    return JSONResponse({"status": "ok", "app": APP_ID})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    if auth.identify(request) is not None:
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": next or "/", "error": error, "configured": auth.is_configured(),
        "build": BUILD})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    user = auth.authenticate(password)
    if user is None:
        # Deliberately vague, and never echoes what was typed back into the page.
        return templates.TemplateResponse(request, "login.html", {
            "next": next or "/", "error": "That password was not accepted.",
            "configured": auth.is_configured(), "build": BUILD}, status_code=401)

    # Only send the redirect somewhere within this site: an open redirect turns the login page
    # into a credential-phishing hop.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue(user),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        # Set only when the request actually arrived over HTTPS — a Secure cookie on plain
        # http://127.0.0.1 is silently dropped, which locks out local use entirely.
        secure=request.url.scheme == "https")
    return response


@app.post("/logout")
@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.exception_handler(Exception)
def handle_unexpected(request: Request, exc: Exception):
    """Record the failure and show what happened, rather than a bare 'Internal Server Error'."""
    errors.record("web", exc, path=str(request.url.path))
    body = f"""
      <h1>Something went wrong</h1>
      <p>The error has been logged. <a href="/errors">See the error log</a> or
         <a href="{request.url.path}">try again</a>.</p>
      <pre>{type(exc).__name__}: {str(exc)[:300]}</pre>
      <p><a href="/">Back to the dashboard</a></p>
    """
    return HTMLResponse(
        f'<!doctype html><html><head><title>Error</title>'
        f'<link rel="stylesheet" href="/static/app.css"></head>'
        f'<body><main>{body}</main></body></html>', status_code=500)


def _pct(value: float | int | None, total: float | int | None) -> float:
    return (value or 0) / total if total else 0.0


templates.env.filters["pct"] = _pct
templates.env.filters["comma"] = lambda v: f"{v:,}" if isinstance(v, (int, float)) else v
templates.env.filters["from_json"] = lambda v: json.loads(v) if v else []


def _relative_age(timestamp: str | None) -> str:
    """'4 min ago' — how fresh the data is, which is what people actually want to know."""
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return ""
    seconds = (datetime.now() - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        hours = seconds / 3600
        return f"{hours:.0f} hr ago" if hours < 24 else "yesterday"
    return f"{int(seconds // 86400)} days ago"


def page_context(conn: sqlite3.Connection, request: Request, snapshot: int | None) -> dict:
    """Shared context: which snapshot is being viewed, and what else is available."""
    available = metrics.snapshots(conn)
    snapshot_id = snapshot or (available[0]["id"] if available else None)
    latest = available[0] if available else None
    return {
        "request": request,
        "snapshots": available,
        "snapshot_id": snapshot_id,
        "current": next((s for s in available if s["id"] == snapshot_id), None),
        "latest": latest,
        "updated_at": latest["snapshot_at"] if latest else None,
        "updated_age": _relative_age(latest["snapshot_at"]) if latest else "",
        # True when looking at history rather than the newest data — the header has to say so,
        # or the numbers read as current when they are not.
        "viewing_history": bool(latest and snapshot_id and snapshot_id != latest["id"]),
        "multi_snapshot": len(available) > 1,
        # Carries the selected snapshot across navigation.
        "qs": lambda: f"?snapshot={snapshot_id}" if snapshot_id else "",
        "build": BUILD,
        # On every page, because "are we even looking at the same data?" is the first question
        # whenever two people compare numbers, and it is not answerable from anything else here.
        "identity": identity.manifest(conn),
        # Spelled for however this copy is running: a packaged build has no
        # `python -m ota_analytics.cli` to offer, and saying otherwise sends someone to install
        # Python to fix a problem they do not have.
        "export_dir": str(config.EXPORT_DIR),
        "ingest_command": config.command_hint("ingest-dir", str(config.EXPORT_DIR)),
        "error_summary": errors.summary(conn),
        "auth": scheduler.auth_status(),
        "agent": scheduler.get_scheduler().state,
        "describe_interval": scheduler.describe_interval,
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, snapshot: int | None = None, window: str = "today",
             model: list[str] = Query(default=[])):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    if not ctx["snapshot_id"]:
        return templates.TemplateResponse(request, "empty.html", ctx)

    # One model selection drives the whole page. Empty means every model, so the default view
    # is the full fleet and narrowing is opt-in.
    selected = [m for m in model if m]
    sid = ctx["snapshot_id"]

    # Change information is the point of the system, so it sits on the front page. It reads
    # from the change log over a chosen period rather than comparing two snapshots, so nothing
    # can slip between a chosen pair of endpoints.
    since, until = registry.window_range(window)
    change = registry.movement_summary(conn, since, until)
    change["window_label"] = registry.window_label(window)

    hourly = metrics.hourly_activity(conn, sid, models=selected)
    ctx.update(
        change=change,
        window=window,
        windows=registry.WINDOWS,
        selected_models=selected,
        all_models=metrics.task_state_by(conn, sid, "model"),
        kpis=metrics.kpis(conn, sid, selected),
        pending=metrics.pending_by_reason(conn, sid, selected),
        by_model=metrics.task_state_by(conn, sid, "model", selected),
        staleness=metrics.staleness_buckets(conn, sid, selected),
        hourly=hourly,
        hourly_total=sum(h["devices"] for h in hourly),
        hourly_peak=max(hourly, key=lambda h: h["devices"]) if hourly else {"devices": 0, "hour": "—"},
        by_model_donut=metrics.model_breakdown(conn, sid),
        status_donut=metrics.status_breakdown(conn, sid, selected),
        task_donut=metrics.task_breakdown(conn, sid, selected),
    )
    return templates.TemplateResponse(request, "overview.html", ctx)


@app.get("/pending", response_class=HTMLResponse)
def pending(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(
        kpis=metrics.kpis(conn, ctx["snapshot_id"]),
        buckets=metrics.pending_by_reason(conn, ctx["snapshot_id"]),
        stuck=metrics.stuck_devices(conn, ctx["snapshot_id"]),
        stalled=registry.stalled_devices(conn) if ctx["multi_snapshot"] else [],
    )
    return templates.TemplateResponse(request, "pending.html", ctx)


@app.get("/firmware", response_class=HTMLResponse)
def firmware(request: Request, snapshot: int | None = None,
             model: list[str] = Query(default=[])):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    selected = [m for m in model if m]
    mix = metrics.firmware_mix(conn, ctx["snapshot_id"], selected)
    ctx.update(
        selected_models=selected,
        models=metrics.task_state_by(conn, ctx["snapshot_id"], "model"),
        mix=mix,
        mix_total=sum(r["devices"] for r in mix),
        kpis=metrics.kpis(conn, ctx["snapshot_id"]),
        targets=metrics.targets(conn),
        gaps=metrics.coverage_gaps(conn, ctx["snapshot_id"]),
    )
    return templates.TemplateResponse(request, "firmware.html", ctx)


@app.get("/changes", response_class=HTMLResponse)
def changes(request: Request, window: str = "today"):
    """Changes over a time window, read from the per-device change log.

    No snapshot pair to choose: the log records every move individually, so a device that
    changed and changed back is still visible — which comparing two endpoints could never
    guarantee, however carefully the pair was picked.
    """
    conn = get_conn()
    ctx = page_context(conn, request, None)
    since = registry.window_since(window)

    ctx.update(
        window=window,
        windows=registry.WINDOWS,
        since=since,
        summary=registry.movement_summary(conn, since),
        moves=registry.firmware_moves(conn, since),
        fallbacks=registry.fallbacks(conn),
        at_base=registry.at_base_firmware(conn),
        segments=registry.fallback_segments(conn),
        registry_summary=registry.summary(conn),
    )
    return templates.TemplateResponse(request, "changes.html", ctx)


@app.get("/reachability", response_class=HTMLResponse)
def reachability(request: Request, snapshot: int | None = None, min_devices: int = 20):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(
        rows=metrics.reachability_by_firmware(conn, ctx["snapshot_id"], min_devices),
        staleness=metrics.staleness_buckets(conn, ctx["snapshot_id"]),
        min_devices=min_devices,
    )
    return templates.TemplateResponse(request, "reachability.html", ctx)


@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(rows=metrics.groups(conn, ctx["snapshot_id"]))
    return templates.TemplateResponse(request, "groups.html", ctx)


# ─── downloads ──────────────────────────────────────────────────────────────

EXPORT_MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _download(payload, fmt: str, stem: str):
    """Wrap exported content with the headers that make a browser save it.

    CSV gets a byte-order mark so Excel reads it as UTF-8 instead of mangling accents. The IMEI
    list must NOT: it is pasted straight into the platform, and a leading invisible character
    would corrupt the first identifier in the list.
    """
    filename = exports.timestamped(stem, fmt)
    if isinstance(payload, bytes):
        body = payload
    elif fmt == "csv":
        body = payload.encode("utf-8-sig")
    else:
        body = payload.encode("utf-8")
    return Response(content=body, media_type=EXPORT_MEDIA.get(fmt, "text/plain"),
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/devices/export")
def devices_export(
    snapshot: int | None = None,
    model: str | None = None,
    firmware: str | None = None,
    status: str | None = None,
    queue_state: str | None = None,
    group: str | None = None,
    changed: str | None = None,
    fallback: str | None = None,
    sort: str = "seen",
    dir: str = "desc",
    format: str = "csv",
):
    """The filtered device list, as a file. Same rows as the page, without pagination."""
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    rows, _ = _device_rows(conn, snapshot_id, model=model, firmware=firmware, status=status,
                           queue_state=queue_state, group=group, changed=changed, fallback=fallback,
                           sort=sort, dir=dir)

    # Spell the tag out in the file: a reader opening this in Excel should not have to know
    # what a 1 in a column called is_fallback means.
    for row in rows:
        row["fallback_tag"] = ""
        if row.get("is_fallback"):
            row["fallback_tag"] = ("FALLBACK — missed target"
                                   if row.get("update_firmware") not in (None, row.get("firmware"))
                                   else "FALLBACK — target is base")

    slug = exports.describe({"m": model, "f": firmware, "s": status,
                             "q": queue_state, "g": group, "c": changed,
                             "fb": fallback})
    stem = f"devices_{slug}" if slug else "devices"

    if format == "txt":
        return _download(exports.to_imei_list(rows), "txt", f"{stem}_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(rows, exports.DEVICE_COLUMNS, "Devices",
                                         identity.manifest(conn)), "xlsx", stem)
    return _download(exports.to_csv(rows, exports.DEVICE_COLUMNS), "csv", stem)


@app.get("/changes/export")
def changes_export(window: str = "today", format: str = "csv", only: str = "all"):
    """Firmware moves in a period — all of them, or just the fallbacks."""
    conn = get_conn()
    since, until = registry.window_range(window)
    moves = registry.firmware_moves(conn, since, until, limit=100_000)

    if only == "fallbacks":
        moves = [m for m in moves if m["is_fallback"]]
    elif only == "unplanned":
        moves = [m for m in moves if m["direction"] == "downgrade" and not m["matched_target"]]

    for move in moves:            # spell out the verdict rather than leaving raw flags
        if move["is_fallback"]:
            move["verdict"] = "fallback to base"
        elif move["direction"] == "downgrade":
            move["verdict"] = "planned rollback" if move["matched_target"] else "unplanned"
        else:
            move["verdict"] = "upgrade"

    stem = f"changes_{window}" + (f"_{only}" if only != "all" else "")
    if format == "txt":
        return _download(exports.to_imei_list(moves), "txt", f"{stem}_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(moves, exports.CHANGE_COLUMNS, "Changes",
                                         identity.manifest(conn)), "xlsx", stem)
    return _download(exports.to_csv(moves, exports.CHANGE_COLUMNS), "csv", stem)


@app.get("/pending/export")
def pending_export(snapshot: int | None = None, format: str = "csv"):
    """The stuck-while-reachable list — the cohort most likely to need action today."""
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    rows = metrics.stuck_devices(conn, snapshot_id, limit=100_000)

    if format == "txt":
        return _download(exports.to_imei_list(rows), "txt", "stuck_online_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(rows, exports.DEVICE_COLUMNS, "Stuck online",
                                         identity.manifest(conn)), "xlsx", "stuck_online")
    return _download(exports.to_csv(rows, exports.DEVICE_COLUMNS), "csv", "stuck_online")


@app.get("/errors", response_class=HTMLResponse)
def error_log(request: Request):
    conn = get_conn()
    ctx = page_context(conn, request, None)
    ctx.update(entries=errors.recent(conn, limit=200), log_path=str(errors.LOG_PATH))
    return templates.TemplateResponse(request, "errors.html", ctx)


@app.post("/errors/clear", response_class=HTMLResponse)
def error_log_clear(request: Request):
    conn = get_conn()
    removed = errors.clear(conn)
    ctx = page_context(conn, request, None)
    ctx.update(entries=[], log_path=str(errors.LOG_PATH),
               notice=f"Cleared {removed} recorded error(s). The text log is untouched.")
    return templates.TemplateResponse(request, "errors.html", ctx)


@app.get("/quality", response_class=HTMLResponse)
def quality(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(issues=metrics.quality_issues(conn, ctx["snapshot_id"]))
    return templates.TemplateResponse(request, "quality.html", ctx)


@app.get("/devices", response_class=HTMLResponse)
def devices(
    request: Request,
    snapshot: int | None = None,
    model: str | None = None,
    firmware: str | None = None,
    status: str | None = None,
    queue_state: str | None = None,
    group: str | None = None,
    changed: str | None = None,
    fallback: str | None = None,
    # Literals, not the constants below: default arguments are evaluated when the function is
    # defined, and those are declared further down the module.
    sort: str = "seen",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    rows, total = _device_rows(
        conn, ctx["snapshot_id"], model=model, firmware=firmware, status=status,
        queue_state=queue_state, group=group, changed=changed, fallback=fallback,
        sort=sort, dir=dir, limit=page_size, offset=(page - 1) * page_size)

    active_filters = {"model": model, "firmware": firmware, "status": status,
                      "queue_state": queue_state, "group": group, "changed": changed,
                      "fallback": fallback}
    query = "&".join(f"{k}={v}" for k, v in active_filters.items() if v)
    if ctx["snapshot_id"]:
        query = f"snapshot={ctx['snapshot_id']}&{query}" if query else f"snapshot={ctx['snapshot_id']}"

    ctx.update(
        rows=rows, total=total, page=page, page_size=page_size,
        pages=max(1, -(-total // page_size)),
        filters=active_filters,
        change_windows=CHANGE_WINDOWS,
        sort=sort if sort in SORTABLE else DEFAULT_SORT,
        dir="asc" if dir == "asc" else "desc",
        base_query=query,
        models=metrics.task_state_by(conn, ctx["snapshot_id"], "model"),
        # Firmware values present in this snapshot, so the filter is a pick-list rather than
        # something to type exactly right.
        firmwares=[dict(r) for r in conn.execute("""
            SELECT firmware AS label, COUNT(*) AS devices FROM device_state
            WHERE snapshot_id = ? AND firmware IS NOT NULL
            GROUP BY firmware ORDER BY devices DESC
        """, (ctx["snapshot_id"],))],
    )
    return templates.TemplateResponse(request, "devices.html", ctx)


# Sortable columns, whitelisted so a query param can never reach the ORDER BY as raw SQL.
SORTABLE = {
    "imei": "d.imei",
    "model": "d.device_model",
    "firmware": "d.firmware",
    "changed": "r.last_fw_change_at",
    "target": "d.update_firmware",
    "config": "d.configuration",
    "hw": "d.hw_ver",
    "status": "d.status",
    "task": "d.queue_state",
    "seen": "d.seen_at",
    "checked": "r.last_checked_at",
}
# Most recently seen first: the devices that just reported are the ones worth looking at.
DEFAULT_SORT, DEFAULT_DIR = "seen", "desc"


def _order_by(sort: str, direction: str) -> str:
    column = SORTABLE.get(sort, SORTABLE[DEFAULT_SORT])
    descending = direction != "asc"
    # Missing values sort last either way — a device that has never reported should not head
    # the list just because its timestamp is NULL.
    return f"{column} IS NULL, {column} {'DESC' if descending else 'ASC'}, d.imei"


# Filter presets for "last update". Values are SQL fragments over the registry row.
CHANGE_WINDOWS = [
    ("1h", "changed in the last hour"),
    ("24h", "changed in the last 24 hours"),
    ("7d", "changed in the last 7 days"),
    ("30d", "changed in the last 30 days"),
    ("never", "never changed"),
]


def _device_rows(conn, snapshot_id, *, model=None, firmware=None, status=None,
                 queue_state=None, group=None, changed=None, fallback=None,
                 sort="seen", dir="desc", limit=None, offset=0):
    """The device list behind both the page and its export.

    Shared deliberately: an export that returned a different set from the table above it would
    send someone to act on the wrong devices.
    """
    where, params = _device_filters(snapshot_id, model, firmware, status, queue_state, group)
    join = ("JOIN device_group g ON g.snapshot_id = d.snapshot_id AND g.imei = d.imei"
            if group else "")
    join += " LEFT JOIN device r ON r.imei = d.imei"
    if changed:
        where += " AND " + _changed_clause(changed)

    # The fallback tag, from the single shared definition: task completed, sitting on base.
    rule = metrics.fallback_rule("d")
    tag_sql = f"CASE WHEN {rule} THEN 1 ELSE 0 END AS is_fallback"

    if fallback == "yes":
        where += f" AND ({rule})"
    elif fallback == "missed":
        where += f" AND ({rule}) AND {metrics.missed_target_rule('d')}"

    total = conn.execute(
        f"SELECT COUNT(*) FROM device_state d {join} WHERE {where}", params).fetchone()[0]

    sql = f"""
        SELECT d.imei, d.device_model, d.firmware, d.hw_ver, d.status, d.queue_state, d.queue,
               d.seen_at, d.seen_age_hours, d.configuration, d.groups_raw, d.vin, d.iccid,
               d.update_firmware, d.base_firmware,
               {tag_sql},
               r.prev_firmware, r.last_changed_at, r.last_fw_change_at, r.last_checked_at,
               r.changes AS change_count
        FROM device_state d {join} WHERE {where}
        ORDER BY {_order_by(sort, dir)}
    """
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    return [dict(r) for r in conn.execute(sql, params)], total


def _changed_clause(window: str) -> str:
    if window == "never":
        return "r.last_changed_at IS NULL"
    hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(window)
    if not hours:
        return "1 = 1"
    return (f"r.last_changed_at >= datetime('now', 'localtime', '-{hours} hours')")


def _device_filters(snapshot_id, model, firmware, status, queue_state, group):
    clauses = ["d.snapshot_id = ?"]
    params: list = [snapshot_id]
    for column, value in (("d.device_model", model), ("d.firmware", firmware),
                          ("d.status", status), ("d.queue_state", queue_state),
                          ("g.group_name", group)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(clauses), params


# ─── updating the data ──────────────────────────────────────────────────────

def _is_local(request: Request) -> bool:
    """Whether this request came from the machine itself.

    Matters because the update form takes a platform password: over a plain-HTTP connection
    from another machine that password crosses the network in the clear.
    """
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _update_context(request: Request, **extra) -> dict:
    conn = get_conn()
    ctx = page_context(conn, request, None)
    ctx.update(
        connection=sources.load_connection(),
        presets=sources.PRESETS,
        credential_store=sources.credential_store_name(),
        export_dir=str(config.EXPORT_DIR),
        is_local=_is_local(request),
        startup=startup.status(),
        startup_available=startup.AUTO_START_AVAILABLE,
        **extra,
    )
    return ctx


def _ingest_path(path) -> dict:
    """Ingest a newly-acquired file and rebuild everything that depends on it.

    A duplicate is discarded rather than kept: uploading the same export twice would otherwise
    leave a second 22 MB copy in the folder forever, and it carries no information.
    """
    conn = get_conn()
    result = ingest.ingest_file(conn, path)
    if result.status == "already_ingested":
        try:
            path.unlink()
        except OSError:
            pass
        return {"level": "warn",
                "message": f"That export is identical to one already loaded "
                           f"(snapshot {result.snapshot_id}). Nothing changed, and the "
                           f"duplicate copy was discarded."}

    rollup.rollup_snapshot(conn, result.snapshot_id)
    transitions = None
    detail = (f"{result.rows:,} devices loaded as snapshot {result.snapshot_id}, "
              f"dated {result.snapshot_at}.")
    if transitions:
        detail += f" {transitions:,} device changes computed against the previous snapshot."
    else:
        detail += " This is the first snapshot, so there is nothing to compare against yet."
    if result.ts_source != "filename":
        detail += (" Warning: the snapshot time came from the file date, not the filename, "
                   "so trend spacing may be inaccurate.")
    return {"level": "ok", "message": detail}


def _ingest_records(records: list[dict], source_url: str) -> dict:
    """Load device records pulled straight from the API — no spreadsheet involved."""
    conn = get_conn()
    result = ingest.ingest_records(conn, records, source_name=f"API {source_url}")
    if result.status == "already_ingested":
        return {"level": "warn",
                "message": f"The API returned data identical to snapshot "
                           f"{result.snapshot_id}. Nothing has changed on the platform since "
                           f"that pull, so no new snapshot was created."}

    rollup.rollup_snapshot(conn, result.snapshot_id)
    transitions = None
    message = (f"Pulled {result.rows:,} devices from the API as snapshot "
               f"{result.snapshot_id}.")
    if transitions:
        message += f" {transitions:,} device changes computed against the previous snapshot."
    if result.unknown_columns:
        message += (" Unmapped fields ignored: "
                    + ", ".join(result.unknown_columns[:8]) + ".")
    return {"level": "ok", "message": message}


@app.get("/update", response_class=HTMLResponse)
def update_page(request: Request):
    return templates.TemplateResponse(request, "update.html", _update_context(request))


@app.post("/update/import", response_class=HTMLResponse)
async def update_import(request: Request, file: UploadFile = File(...)):
    try:
        content = await file.read()
        path = sources.store_upload(file.filename or "", content)
        result = _ingest_path(path)
    except (sources.SourceError, ingest.IngestError) as exc:
        result = {"level": "error", "message": str(exc)}
    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, result=result, tab="import"))


@app.post("/update/online", response_class=HTMLResponse)
def update_online(
    request: Request,
    preset: str = Form(sources.DEFAULT_PRESET),
    url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    auth_mode: str = Form("token"),
    login_url: str = Form(""),
    user_field: str = Form("username"),
    pass_field: str = Form("password"),
    login_encoding: str = Form("multipart"),
    password_hash: str = Form("md5"),
    verify_tls: bool = Form(False),
    remember: bool = Form(False),
):
    connection = sources.Connection(
        preset=preset if preset in sources.PRESETS else sources.DEFAULT_PRESET,
        url=url.strip(), username=username.strip(), auth_mode=auth_mode,
        login_url=login_url.strip(), verify_tls=verify_tls,
        user_field=user_field.strip() or "username",
        pass_field=pass_field.strip() or "password",
        login_encoding=login_encoding, password_hash=password_hash,
    )
    # A known platform supplies its own endpoints, so nothing typed here can misconfigure it.
    connection = sources.apply_preset(connection)

    # An empty password field with a stored credential means "use the saved one" — so the
    # password does not have to be retyped on every refresh.
    secret = password or (sources.load_password(connection.username) or "")

    try:
        if not secret and auth_mode != "none":
            raise sources.SourceError("No password given, and none is saved for this user.")

        sources.save_connection(connection)
        if remember and secret:
            if not sources.save_password(connection.username, secret):
                raise sources.SourceError(
                    "Could not save the password to the OS credential store. The download was "
                    "not attempted — re-run without 'remember me' to continue without saving.")
        elif not remember:
            sources.forget_password(connection.username)

        fetched = sources.fetch_export(connection, secret)
        if fetched.records is not None:
            result = _ingest_records(fetched.records, connection.url)
        else:
            result = _ingest_path(fetched.path)
            result["message"] = f"Downloaded {fetched.path.name}. " + result["message"]
    except (sources.SourceError, ingest.IngestError) as exc:
        result = {"level": "error", "message": str(exc)}

    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, result=result, tab="online"))


@app.post("/update/schedule", response_class=HTMLResponse)
def update_schedule(request: Request,
                    enabled: bool = Form(False),
                    interval_value: int = Form(1),
                    interval_unit: str = Form("hours")):
    multiplier = {"minutes": 60, "hours": 3600}.get(interval_unit, 3600)
    seconds = scheduler.clamp_interval(interval_value * multiplier)
    agent = scheduler.get_scheduler()
    state = agent.configure(enabled=enabled, interval_seconds=seconds)

    if enabled:
        message = (f"Auto-fetch on — every {scheduler.describe_interval(state.interval_seconds)}. "
                   f"Next run at {state.next_run}.")
        level = "ok"
        if not _update_context(request)["auth"]["can_automate"]:
            message += (" Note: the saved credentials cannot be renewed automatically, so this "
                        "will stop working once the token expires.")
            level = "warn"
    else:
        message, level = "Auto-fetch off.", "ok"

    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="agent", result={"level": level, "message": message}))


@app.post("/update/startup", response_class=HTMLResponse)
def update_startup(request: Request, enabled: bool = Form(False),
                   startup_delay: int = Form(startup.DEFAULT_DELAY_MINUTES)):
    """Turn 'start with Windows' on or off.

    Still reachable while the feature is withdrawn, because the route outlives the button: a
    bookmark or an old page left open in a tab would otherwise re-arm the very thing being
    removed. Turning it *off* is always allowed.
    """
    if enabled and not startup.AUTO_START_AVAILABLE:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="agent", result={
                "level": "warn",
                "message": "Start with Windows has been removed. It ran a copy of the "
                           "dashboard with no window, which held the port and could not be "
                           "seen or stopped. Auto-fetch still runs on its schedule whenever "
                           "the app is open."}))

    state = startup.enable(startup_delay) if enabled else startup.disable()

    if not state.supported:
        result = {"level": "warn", "message": "Starting with the system is only available "
                                              "on Windows."}
    elif state.enabled:
        when = "after the machine boots" if state.starts_at_boot else "after you sign in"
        how = ("a scheduled task — it runs even if nobody signs in"
               if state.starts_at_boot else
               "a Startup-folder entry; this machine refused to register a scheduled task, so "
               "it needs someone signed in")
        message = (f"The dashboard will start with Windows, {state.delay_minutes} minutes "
                   f"{when}, and resume fetching on its schedule. Using {how}.")
        result = {"level": "warn" if state.warning else "ok",
                  "message": message + (f" {state.warning}" if state.warning else "")}
    else:
        result = {"level": "ok", "message": "The dashboard will no longer start with Windows."}

    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, tab="agent", result=result))


@app.post("/update/startup-test", response_class=HTMLResponse)
def update_startup_test(request: Request):
    """Launch exactly what auto-start launches, without waiting for a reboot.

    The whole point of this feature is that it works when nobody is watching, so being able to
    prove it before trusting it matters more than usual.
    """
    started = startup.run_now()
    port = request.url.port or 8000
    result = ({"level": "ok",
               "message": f"Launched the same command the startup entry uses. If it is working "
                          f"you now have a second copy running — check http://127.0.0.1:{port} "
                          f"in a moment, then close the extra one."}
              if started else
              {"level": "error", "message": "Could not launch it. See the error log."})
    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, tab="agent", result=result))


@app.post("/update/run-now", response_class=HTMLResponse)
def update_run_now(request: Request):
    state = scheduler.get_scheduler().run_now()
    level = {"ok": "ok", "unchanged": "warn", "error": "error"}.get(state.last_status, "warn")
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="agent", result={"level": level, "message": state.last_message}))


@app.get("/api/agent")
def api_agent():
    """Status for the header bar; polled so the countdown stays honest."""
    agent = scheduler.get_scheduler()
    return JSONResponse({**agent.state.to_dict(),
                         "interval_label": scheduler.describe_interval(
                             agent.state.interval_seconds),
                         "auth": scheduler.auth_status()})


# ─── sharing the database ───────────────────────────────────────────────────

@app.get("/update/bundle")
def update_bundle_export(since: str | None = None):
    """Download this install's snapshot history for someone else to merge.

    Built in memory rather than written to disk: a bundle is a point-in-time copy, and leaving
    them lying around in the data folder would accumulate stale ones that look current.
    """
    conn = get_conn()
    buffer = io.BytesIO()
    bundle.export_bundle(conn, buffer, since=since or None)
    return Response(
        content=buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{bundle.suggested_filename(conn)}"'})


@app.post("/update/bundle-import", response_class=HTMLResponse)
async def update_bundle_import(request: Request, file: UploadFile = File(...),
                               allow_interleave: bool = Form(False)):
    conn = get_conn()
    try:
        outcome = bundle.import_bundle(conn, await file.read(),
                                       allow_interleave=allow_interleave)
        # "Already loaded" is a correct answer, not a problem: showing it as a warning made a
        # successful no-op read as a failure.
        level = {"imported": "ok", "already_present": "ok",
                 "refused": "warn", "empty": "warn"}.get(outcome.status, "warn")
        result = {"level": level, "message": outcome.message}
    except bundle.BundleError as exc:
        result = {"level": "error", "message": str(exc)}
    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, result=result, tab="share"))


@app.post("/update/label", response_class=HTMLResponse)
def update_label(request: Request, label: str = Form("")):
    """Name this install, so a shared report says whose numbers it is."""
    conn = get_conn()
    name = identity.set_instance_label(conn, label)
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="share",
        result={"level": "ok", "message": f"This install is now called {name!r}. It appears on "
                                          f"every bundle and report it produces."}))


@app.get("/api/identity")
def api_identity():
    """What this database is and exactly what it holds — the reconciliation endpoint."""
    return JSONResponse(identity.manifest(get_conn()))


@app.post("/update/forget", response_class=HTMLResponse)
def update_forget(request: Request, username: str = Form("")):
    sources.forget_password(username)
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="online",
        result={"level": "ok", "message": f"Saved password for {username!r} deleted from the "
                                          "OS credential store."}))


# ─── JSON API ───────────────────────────────────────────────────────────────

@app.get("/api/version")
def api_version():
    """Identify exactly what is running — for bug reports and deployment checks."""
    from . import VERSION_HISTORY

    conn = get_conn()
    latest = metrics.snapshots(conn)
    return JSONResponse({
        **BUILD,
        "snapshots": len(latest),
        "latest_snapshot": latest[0]["snapshot_at"] if latest else None,
        "history": [{"version": v, "released": d, "summary": s} for v, d, s in VERSION_HISTORY],
    })


@app.get("/api/kpis")
def api_kpis(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.kpis(conn, snapshot_id))


@app.get("/api/pending")
def api_pending(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse({"buckets": metrics.pending_by_reason(conn, snapshot_id),
                         "stuck": metrics.stuck_devices(conn, snapshot_id)})


@app.get("/api/firmware-mix")
def api_firmware_mix(snapshot: int | None = None, model: str | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.firmware_mix(conn, snapshot_id, model))


@app.get("/api/reachability")
def api_reachability(snapshot: int | None = None, min_devices: int = 20):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.reachability_by_firmware(conn, snapshot_id, min_devices))


@app.get("/api/quality")
def api_quality(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.quality_issues(conn, snapshot_id))


@app.get("/api/changes")
def api_changes(window: str = "today"):
    """Movement over a period, from the change log — replaces the old transition endpoint."""
    conn = get_conn()
    since, until = registry.window_range(window)
    return JSONResponse({
        "window": window,
        "summary": registry.movement_summary(conn, since, until),
        "moves": registry.firmware_moves(conn, since, until, limit=200),
        "fallbacks": registry.fallbacks(conn, limit=100),
    })


