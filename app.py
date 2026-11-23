#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import os
import hmac
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template_string, request, send_file

import xfdl

app = Flask(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS = {}
JOBS_LOCK = threading.Lock()


def _resolve_root_dir(out_dir=None):
    # All downloads and vault operations stay inside this app's downloads folder.
    raw = (out_dir or "./downloads").strip().replace("\\", "/")
    if not raw:
        raw = "./downloads"
    if os.path.isabs(raw):
        raise ValueError("Output folder must be inside ./downloads.")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if parts and parts[0] == "downloads":
        parts = parts[1:]
    if any(part == ".." for part in parts):
        raise ValueError("Output folder cannot contain parent-directory segments.")
    root = os.path.abspath(os.path.join(APP_DIR, "downloads", *parts))
    safe_root = os.path.abspath(os.path.join(APP_DIR, "downloads"))
    if root != safe_root and not root.startswith(safe_root + os.sep):
        raise ValueError("Output folder must be inside ./downloads.")
    return root


@app.before_request
def require_app_password():
    if request.endpoint == "healthz":
        return None
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return jsonify({"error": "APP_PASSWORD is not configured on the server."}), 503
    supplied = request.authorization.password if request.authorization else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        response = jsonify({"error": "Authentication required."})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="XFDL Mobile Downloader"'
        return response
    return None


def _safe_child_path(root_dir, rel_path):
    cleaned = (rel_path or "").lstrip("/").replace("\\", "/")
    if not cleaned or ".." in cleaned.split("/"):
        return None
    target = os.path.abspath(os.path.join(root_dir, cleaned))
    root_abs = os.path.abspath(root_dir)
    if target == root_abs or not target.startswith(root_abs + os.sep):
        return None
    return target


def _collect_vault_tree(root_dir):
    if not os.path.isdir(root_dir):
        return {"root": root_dir, "folders": [], "files": []}

    folders = []
    files = []
    root_abs = os.path.abspath(root_dir)

    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames.sort()
        filenames.sort()
        rel_dir = os.path.relpath(dirpath, root_abs)
        if rel_dir == ".":
            rel_dir = ""
        else:
            rel_dir = rel_dir.replace("\\", "/")

        dir_file_count = 0
        dir_bytes = 0
        for fname in filenames:
            if fname.endswith(".part") or fname.startswith("."):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            if not os.path.isfile(fpath):
                continue
            rel_file = f"{rel_dir}/{fname}" if rel_dir else fname
            files.append({
                "name": fname,
                "folder": rel_dir or "(root)",
                "path": rel_file,
                "size": st.st_size,
                "size_fmt": xfdl.fmt_size(st.st_size),
            })
            dir_file_count += 1
            dir_bytes += st.st_size

        if rel_dir:
            folders.append({
                "name": rel_dir,
                "files": dir_file_count,
                "size": dir_bytes,
                "size_fmt": xfdl.fmt_size(dir_bytes),
            })

    return {"root": root_abs, "folders": folders, "files": files}


def _create_job(payload):
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "status": "queued",
        "stage": "QUEUED",
        "thread_title": "",
        "pages": "0..0",
        "candidates_total": 0,
        "candidates_done": 0,
        "resolved_total": 0,
        "dl_total": 0,
        "dl_done": 0,
        "downloaded": 0,
        "skipped": 0,
        "errors": 0,
        "elapsed": "00:00",
        "out_dir": "",
        "logs": [],
        "created_at": time.monotonic(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    t = threading.Thread(target=_run_job, args=(job_id, payload), daemon=True)
    t.start()
    return job


def _append_log(job, tag, msg):
    stamp = time.strftime("%H:%M:%S")
    with JOBS_LOCK:
        job["logs"].append({"time": stamp, "tag": tag, "msg": msg})
        if len(job["logs"]) > 250:
            job["logs"] = job["logs"][-250:]


def _run_job(job_id, payload):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["stage"] = "SCRAPE"

    t0 = time.monotonic()

    def touch_elapsed():
        with JOBS_LOCK:
            job["elapsed"] = xfdl.fmt_eta(time.monotonic() - t0)

    try:
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("Missing thread URL")
        parsed_thread = urlparse(url)
        host = (parsed_thread.hostname or "").lower()
        if parsed_thread.scheme != "https" or not (host == "simpcity.cr" or host.endswith(".simpcity.cr")):
            raise ValueError("Use an HTTPS SimpCity thread URL on simpcity.cr.")

        cookie_str = (payload.get("cookie") or "").strip()
        if not cookie_str:
            raise ValueError("Cookie input is required")

        out_dir = _resolve_root_dir(payload.get("out_dir"))
        media_filter = payload.get("media") or "all"
        workers = max(1, min(int(payload.get("workers") or 4), 8))
        resolve_workers = max(1, min(int(payload.get("resolve_workers") or 6), 12))
        dl_workers = max(1, min(int(payload.get("dl_workers") or 6), 12))
        page_delay = max(
            0.0,
            float(payload.get("page_delay") if payload.get("page_delay") is not None else 0.15),
        )
        links_only = bool(payload.get("links_only"))
        resume = bool(payload.get("resume"))
        no_keepalive = bool(payload.get("no_keepalive"))
        ua_str = (payload.get("user_agent") or "").strip()

        dl = xfdl.Downloader(
            cookie_header=cookie_str,
            user_agent=ua_str,
            out_dir=out_dir,
            page_delay=page_delay,
            no_keepalive=no_keepalive,
        )
        # Scope the forum session cookie to SimpCity only. Resolved media hosts
        # must never receive the forum login cookie.
        dl.s.headers.pop("Cookie", None)
        clean_cookie = xfdl.parse_cookie_input(cookie_str)
        for pair in clean_cookie.split(";"):
            name, separator, value = pair.strip().partition("=")
            if separator and name.strip():
                dl.s.cookies.set(name.strip(), value.strip(), domain=".simpcity.cr", path="/", secure=True)

        _append_log(job, "SCRAPE", f"Hitting thread: {url}")
        performer, raw_entries = dl.scrape_thread(
            url,
            max_pages=500,
            single_page=bool(payload.get("single_page")),
            include_quotes=bool(payload.get("include_quotes")),
            workers=workers,
        )
        dest_folder = os.path.join(dl.out_dir, performer)
        links_path = os.path.join(dest_folder, "links.txt")

        with JOBS_LOCK:
            job["thread_title"] = performer
            job["out_dir"] = dest_folder
            job["candidates_total"] = len(raw_entries)
            job["stage"] = "RESOLVE"
        touch_elapsed()
        _append_log(job, "SCRAPE", f"Locked '{performer}' — {len(raw_entries)} candidate links")

        resolved_urls = xfdl.load_existing_links(links_path) if resume else []
        seen = set(resolved_urls)
        resolve_errors = 0
        done_count = 0

        links_fh = None
        if links_only:
            os.makedirs(dest_folder, exist_ok=True)
            links_fh = open(links_path, "a" if resume else "w", encoding="utf-8")

        try:
            if raw_entries:
                with ThreadPoolExecutor(max_workers=resolve_workers) as pool:
                    futs = [
                        pool.submit(
                            xfdl._resolve_single_candidate,
                            dl,
                            idx,
                            len(raw_entries),
                            cand_url,
                            spoilers,
                            media_filter,
                        )
                        for idx, (cand_url, spoilers) in enumerate(raw_entries, 1)
                    ]
                    for fut in as_completed(futs):
                        idx, cand_url, found_urls, elapsed, err = fut.result()
                        done_count += 1
                        added = 0
                        if err is not None:
                            resolve_errors += 1
                            _append_log(job, "ERR", f"[{idx}] {type(err).__name__}: {cand_url[:50]}")
                        else:
                            for u in found_urls:
                                if u not in seen:
                                    seen.add(u)
                                    resolved_urls.append(u)
                                    added += 1
                                    if links_fh is not None:
                                        links_fh.write(u + "\n")
                            if added > 0 and links_fh is not None:
                                links_fh.flush()
                            if elapsed > 3.0:
                                _append_log(job, "SLOW", f"[{idx}] {elapsed:.1f}s <- {cand_url[:48]}")
                            elif added > 0:
                                _append_log(job, "OK", f"[{idx}] +{added} <- {cand_url[:48]}")

                        with JOBS_LOCK:
                            job["candidates_done"] = done_count
                            job["resolved_total"] = len(resolved_urls)
                            job["errors"] = resolve_errors
                        touch_elapsed()
        finally:
            if links_fh is not None:
                links_fh.close()

        if links_only:
            os.makedirs(dest_folder, exist_ok=True)
            if not resume:
                with open(links_path, "w", encoding="utf-8") as f:
                    if resolved_urls:
                        f.write("\n".join(resolved_urls) + "\n")
            with JOBS_LOCK:
                job["status"] = "completed"
                job["stage"] = "LINKS READY"
            touch_elapsed()
            _append_log(job, "DONE", f"Exported {len(resolved_urls)} links to {links_path}")
            return

        with JOBS_LOCK:
            job["stage"] = "DOWNLOAD"
            job["dl_total"] = len(resolved_urls)
            job["dl_done"] = 0
        touch_elapsed()
        _append_log(job, "DL", f"Dropping {len(resolved_urls)} files with {dl_workers} workers")

        dl_done = 0
        dl_ok = 0
        dl_skip = 0
        dl_err = 0

        if resolved_urls:
            with ThreadPoolExecutor(max_workers=dl_workers) as pool:
                futs = [
                    pool.submit(
                        dl.download_file,
                        u,
                        dest_folder,
                        f"[{i}/{len(resolved_urls)}]",
                        False,
                    )
                    for i, u in enumerate(resolved_urls, 1)
                ]
                for fut in as_completed(futs):
                    dl_done += 1
                    try:
                        st = fut.result()
                    except Exception as exc:
                        st = "error"
                        _append_log(job, "ERR", f"{type(exc).__name__}: {exc}")
                    if st == "downloaded":
                        dl_ok += 1
                    elif st == "skipped":
                        dl_skip += 1
                    else:
                        dl_err += 1

                    with JOBS_LOCK:
                        job["dl_done"] = dl_done
                        job["downloaded"] = dl_ok
                        job["skipped"] = dl_skip
                        job["errors"] = resolve_errors + dl_err
                    touch_elapsed()

        with JOBS_LOCK:
            job["status"] = "completed"
            job["stage"] = "COMPLETED"
            job["errors"] = resolve_errors + dl_err
        touch_elapsed()
        _append_log(
            job,
            "DONE",
            f"Finished — {dl_ok} downloaded, {dl_skip} skipped, {resolve_errors + dl_err} errors",
        )

    except SystemExit:
        with JOBS_LOCK:
            job["status"] = "failed"
            job["stage"] = "AUTH / LOAD ERR"
        touch_elapsed()
        _append_log(job, "ERR", "Thread load failed (check pasted cookie / session)")
    except Exception as e:
        with JOBS_LOCK:
            job["status"] = "failed"
            job["stage"] = "CRASHED"
        touch_elapsed()
        _append_log(job, "ERR", f"{type(e).__name__}: {e}")


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>XFDL Mobile Downloader</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Anton&family=JetBrains+Mono:wght@500;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #070709;
      --surface: #101016;
      --surface-2: #171720;
      --ink: #f6f6f9;
      --muted: #8b8b9c;
      --volt: #d9ff00;
      --hot: #ff1e6e;
      --cyan: #00f0ff;
      --ok: #22ff88;
      --line: rgba(255, 255, 255, 0.11);
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body {
      margin: 0;
      padding: 14px 14px 52px;
      background:
        radial-gradient(circle at 88% -6%, rgba(255, 30, 110, 0.22), transparent 42%),
        radial-gradient(circle at 12% 4%, rgba(217, 255, 0, 0.14), transparent 38%),
        var(--bg);
      color: var(--ink);
      font-family: 'Space Grotesk', system-ui, sans-serif;
      min-height: 100vh;
    }
    .wrap { max-width: 520px; margin: 0 auto; }
    .masthead {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 10px;
      padding: 6px 2px 14px;
      border-bottom: 2px solid var(--line);
    }
    .brand-tag {
      display: inline-block;
      background: var(--volt);
      color: #070709;
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 10px;
      letter-spacing: 0.14em;
      padding: 3px 7px;
      transform: rotate(-2deg);
      margin-bottom: 5px;
    }
    h1 {
      margin: 0;
      font-family: 'Anton', Impact, sans-serif;
      font-size: 38px;
      line-height: 0.94;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    h1 span { color: var(--volt); }
    .tour-pill {
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      font-weight: 700;
      color: var(--hot);
      border: 1px solid rgba(255, 30, 110, 0.5);
      background: rgba(255, 30, 110, 0.1);
      padding: 5px 8px;
      border-radius: 999px;
      white-space: nowrap;
    }
    .card {
      margin-top: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 14px 36px rgba(0, 0, 0, 0.45);
    }
    .label-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }
    label {
      display: block;
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .label-row label { margin-bottom: 0; }
    .req-tag {
      color: var(--volt);
      font-family: 'JetBrains Mono', monospace;
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 0.1em;
    }
    .mini-btn {
      background: var(--surface-2);
      color: var(--hot);
      border: 1px solid rgba(255, 30, 110, 0.4);
      border-radius: 7px;
      padding: 4px 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 9px;
      font-weight: 800;
      cursor: pointer;
    }
    .mini-btn.cyan-btn {
      color: var(--cyan);
      border-color: rgba(0, 240, 255, 0.4);
    }
    .hint-note {
      margin-top: 5px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      color: var(--muted);
    }
    .alert-box {
      display: none;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(255, 30, 110, 0.14);
      border: 1px solid var(--hot);
      color: #ff9ec0;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
    }
    input[type="text"], input[type="number"], select, textarea {
      width: 100%;
      background: var(--surface-2);
      color: var(--ink);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 11px;
      padding: 11px 12px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      outline: none;
    }
    input:focus, select:focus, textarea:focus {
      border-color: var(--volt);
      box-shadow: 0 0 0 2px rgba(217, 255, 0, 0.16);
    }
    .field-gap { margin-top: 12px; }
    .grid-3 {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 9px;
      margin-top: 12px;
    }
    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 9px;
      margin-top: 12px;
    }
    .toggles {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
    }
    .chip input { accent-color: var(--volt); margin: 0; }
    details {
      margin-top: 12px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
    }
    summary {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 700;
      color: var(--cyan);
      cursor: pointer;
      list-style: none;
    }
    .drop-btn {
      margin-top: 14px;
      width: 100%;
      border: none;
      border-radius: 14px;
      padding: 15px 18px;
      background: linear-gradient(90deg, var(--volt) 0%, #b4ff00 100%);
      color: #070709;
      font-family: 'Anton', Impact, sans-serif;
      font-size: 22px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      cursor: pointer;
      box-shadow: 0 8px 24px rgba(217, 255, 0, 0.25);
    }
    .drop-btn:active { transform: scale(0.99); }
    .drop-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .meter-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 800;
    }
    .stage-badge {
      background: rgba(0, 240, 255, 0.14);
      color: var(--cyan);
      border: 1px solid rgba(0, 240, 255, 0.4);
      padding: 3px 8px;
      border-radius: 6px;
    }
    .bar-track {
      margin-top: 8px;
      height: 12px;
      background: var(--surface-2);
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--line);
    }
    .bar-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--hot), var(--volt));
      transition: width 0.25s ease;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin-top: 12px;
    }
    .stat {
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 11px;
      padding: 9px 8px;
      text-align: center;
    }
    .stat-val {
      font-family: 'JetBrains Mono', monospace;
      font-size: 16px;
      font-weight: 800;
      color: var(--volt);
    }
    .stat-lbl {
      font-family: 'JetBrains Mono', monospace;
      font-size: 9px;
      color: var(--muted);
      margin-top: 2px;
      text-transform: uppercase;
    }
    .thread-line {
      margin-top: 10px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .console {
      margin-top: 12px;
      background: #050507;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      max-height: 220px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      line-height: 1.45;
    }
    .log-row { margin-bottom: 3px; word-break: break-all; }
    .log-t { color: var(--muted); }
    .log-tag { color: var(--volt); font-weight: 700; margin: 0 5px; }
    .log-tag.ERR { color: var(--hot); }
    .log-tag.DONE, .log-tag.OK { color: var(--ok); }
    .vault-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }
    .vault-title {
      font-family: 'Anton', Impact, sans-serif;
      font-size: 20px;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .vault-title span { color: var(--cyan); }
    .folder-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 10px;
    }
    .folder-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 5px 9px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      color: var(--ink);
      cursor: pointer;
    }
    .folder-pill.active {
      border-color: var(--volt);
      color: var(--volt);
    }
    .zip-link {
      color: var(--hot);
      text-decoration: none;
      font-weight: 800;
    }
    .file-list {
      background: #050507;
      border: 1px solid var(--line);
      border-radius: 12px;
      max-height: 260px;
      overflow-y: auto;
    }
    .file-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 9px 11px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
    }
    .file-row:last-child { border-bottom: none; }
    .file-meta {
      min-width: 0;
      flex: 1;
    }
    .file-name {
      color: var(--ink);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .file-sub {
      color: var(--muted);
      font-size: 9px;
      margin-top: 2px;
    }
    .dl-pill {
      flex-shrink: 0;
      background: rgba(217, 255, 0, 0.14);
      color: var(--volt);
      border: 1px solid rgba(217, 255, 0, 0.45);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 10px;
      font-weight: 800;
      text-decoration: none;
    }
    .empty-vault {
      padding: 18px 12px;
      text-align: center;
      color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="masthead">
      <div>
        <div class="brand-tag">MOBILE TOUR DECK // V1</div>
        <h1>XFDL <span>MOBILE</span></h1>
      </div>
      <div class="tour-pill" id="clock-pill">STANDBY</div>
    </div>

    <form class="card" id="run-form">
      <div class="label-row">
        <label for="cookie">Session Cookie (Netscape or Header)</label>
        <span class="req-tag">* REQUIRED</span>
      </div>
      <textarea id="cookie" name="cookie" rows="4" required placeholder="Paste Netscape cookie export or name=val; name2=val2 here..."></textarea>
      <div class="label-row" style="margin-top: 6px;">
        <span class="hint-note" style="margin-top: 0;">Sent per job — never written to repo or disk.</span>
        <button type="button" class="mini-btn" id="wipe-cookie-btn">WIPE SAVED</button>
      </div>

      <div class="field-gap">
        <label for="url">Thread / Page URL</label>
        <input id="url" name="url" type="text" required placeholder="https://simpcity.cr/threads/..." />
      </div>

      <div class="grid-3">
        <div>
          <label for="workers">Scrape W</label>
          <input id="workers" name="workers" type="number" min="1" max="8" value="4" />
        </div>
        <div>
          <label for="resolve_workers">Resolve W</label>
          <input id="resolve_workers" name="resolve_workers" type="number" min="1" max="12" value="6" />
        </div>
        <div>
          <label for="dl_workers">DL W</label>
          <input id="dl_workers" name="dl_workers" type="number" min="1" max="12" value="6" />
        </div>
      </div>

      <div class="grid-2">
        <div>
          <label for="media">Media Filter</label>
          <select id="media" name="media">
            <option value="all" selected>ALL MEDIA</option>
            <option value="video">VIDEO ONLY</option>
            <option value="image">IMAGE ONLY</option>
          </select>
        </div>
        <div>
          <label for="out_dir">Output Folder</label>
          <input id="out_dir" name="out_dir" type="text" value="./downloads" />
        </div>
      </div>

      <div class="toggles">
        <label class="chip"><input type="checkbox" id="save_local" /> REMEMBER COOKIE ON THIS DEVICE</label>
        <label class="chip"><input type="checkbox" id="resume" name="resume" checked /> RESUME</label>
        <label class="chip"><input type="checkbox" id="links_only" name="links_only" /> LINKS ONLY</label>
        <label class="chip"><input type="checkbox" id="no_keepalive" name="no_keepalive" /> NO KEEPALIVE</label>
        <label class="chip"><input type="checkbox" id="single_page" name="single_page" /> SINGLE PAGE</label>
      </div>

      <details>
        <summary>+ CUSTOM USER-AGENT (OPTIONAL)</summary>
        <div style="margin-top: 10px;">
          <label for="user_agent">Custom User-Agent</label>
          <input id="user_agent" name="user_agent" type="text" placeholder="Leave blank to use default mobile UA" />
        </div>
      </details>

      <div class="alert-box" id="form-alert"></div>
      <button class="drop-btn" id="submit-btn" type="submit">RUN THE SET</button>
    </form>

    <div class="card" id="telemetry" style="display: none;">
      <div class="meter-head">
        <span class="stage-badge" id="stage-badge">SCRAPE</span>
        <span id="pct-label">0%</span>
        <span id="elapsed-label">00:00</span>
      </div>
      <div class="bar-track"><div class="bar-fill" id="bar-fill"></div></div>
      <div class="thread-line" id="thread-line">Thread: —</div>

      <div class="stats">
        <div class="stat"><div class="stat-val" id="st-resolved">0</div><div class="stat-lbl">Resolved</div></div>
        <div class="stat"><div class="stat-val" id="st-dl">0</div><div class="stat-lbl">Saved</div></div>
        <div class="stat"><div class="stat-val" id="st-skip">0</div><div class="stat-lbl">Skipped</div></div>
        <div class="stat"><div class="stat-val" id="st-err" style="color: var(--hot);">0</div><div class="stat-lbl">Errors</div></div>
      </div>

      <div class="console" id="console"></div>
    </div>

    <div class="card" id="vault-card">
      <div class="vault-head">
        <div class="vault-title">SERVER <span>VAULT</span></div>
        <button type="button" class="mini-btn cyan-btn" id="refresh-vault-btn">REFRESH</button>
      </div>
      <div class="folder-bar" id="folder-bar"></div>
      <div class="file-list" id="file-list">
        <div class="empty-vault">Loading server files...</div>
      </div>
    </div>
  </div>

  <script>
    const STORAGE_KEY = "xfdl_session_cookie_v1";
    let activeJobId = null;
    let pollTimer = null;
    let selectedFolder = "__ALL__";
    let vaultCache = { folders: [], files: [] };

    const cookieEl = document.getElementById("cookie");
    const saveLocalEl = document.getElementById("save_local");
    const alertEl = document.getElementById("form-alert");
    const outDirEl = document.getElementById("out_dir");

    // Never restore a forum cookie automatically. Clear any value saved by older builds.
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}

    function syncLocalCookie() {
      try {
        if (saveLocalEl.checked && cookieEl.value.trim()) {
          localStorage.setItem(STORAGE_KEY, cookieEl.value);
        } else if (!saveLocalEl.checked) {
          localStorage.removeItem(STORAGE_KEY);
        }
      } catch (_) {}
    }

    cookieEl.addEventListener("input", syncLocalCookie);
    saveLocalEl.addEventListener("change", syncLocalCookie);
    outDirEl.addEventListener("change", () => loadVault());

    document.getElementById("wipe-cookie-btn").addEventListener("click", () => {
      cookieEl.value = "";
      try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
    });

    document.getElementById("refresh-vault-btn").addEventListener("click", () => loadVault());

    function showAlert(msg) {
      alertEl.textContent = msg;
      alertEl.style.display = "block";
    }

    function hideAlert() {
      alertEl.style.display = "none";
      alertEl.textContent = "";
    }

    function escHtml(s) {
      return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function renderVault() {
      const rootParam = encodeURIComponent(outDirEl.value.trim() || "./downloads");
      const folderBar = document.getElementById("folder-bar");
      const fileList = document.getElementById("file-list");

      const allPill = `<button type="button" class="folder-pill ${selectedFolder === "__ALL__" ? "active" : ""}" data-folder="__ALL__">ALL (${vaultCache.files.length})</button>`;
      const folderPills = (vaultCache.folders || []).map(f => {
        const active = selectedFolder === f.name ? "active" : "";
        const zipHref = `/api/files/zip/${encodeURIComponent(f.name)}?root=${rootParam}`;
        return `<span class="folder-pill ${active}" data-folder="${escHtml(f.name)}">
          <span>${escHtml(f.name)} (${f.files})</span>
          <a class="zip-link" href="${zipHref}" onclick="event.stopPropagation()">ZIP</a>
        </span>`;
      }).join("");

      folderBar.innerHTML = allPill + folderPills;
      folderBar.querySelectorAll("[data-folder]").forEach(el => {
        el.addEventListener("click", () => {
          selectedFolder = el.getAttribute("data-folder");
          renderVault();
        });
      });

      const items = selectedFolder === "__ALL__"
        ? (vaultCache.files || [])
        : (vaultCache.files || []).filter(item => item.folder === selectedFolder);

      if (!items.length) {
        fileList.innerHTML = `<div class="empty-vault">No downloaded files in this view yet.</div>`;
        return;
      }

      fileList.innerHTML = items.map(item => {
        const dlHref = `/api/files/download/${encodeURIComponent(item.path)}?root=${rootParam}`;
        return `<div class="file-row">
          <div class="file-meta">
            <div class="file-name">${escHtml(item.name)}</div>
            <div class="file-sub">${escHtml(item.folder)} · ${escHtml(item.size_fmt)}</div>
          </div>
          <a class="dl-pill" href="${dlHref}" download>GET</a>
        </div>`;
      }).join("");
    }

    async function loadVault() {
      const rootParam = encodeURIComponent(outDirEl.value.trim() || "./downloads");
      try {
        const res = await fetch(`/api/files?root=${rootParam}`);
        if (!res.ok) return;
        vaultCache = await res.json();
        if (
          selectedFolder !== "__ALL__" &&
          !(vaultCache.folders || []).some(f => f.name === selectedFolder)
        ) {
          selectedFolder = "__ALL__";
        }
        renderVault();
      } catch (_) {}
    }

    document.getElementById("run-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      hideAlert();

      const cookieVal = cookieEl.value.trim();
      const urlVal = document.getElementById("url").value.trim();
      if (!cookieVal) {
        showAlert("Paste your session cookie first — jobs require a cookie input.");
        cookieEl.focus();
        return;
      }
      if (!urlVal) {
        showAlert("Enter a thread URL first.");
        return;
      }

      syncLocalCookie();

      const btn = document.getElementById("submit-btn");
      btn.disabled = true;

      const payload = {
        url: urlVal,
        cookie: cookieVal,
        workers: Number(document.getElementById("workers").value || 4),
        resolve_workers: Number(document.getElementById("resolve_workers").value || 6),
        dl_workers: Number(document.getElementById("dl_workers").value || 6),
        media: document.getElementById("media").value,
        out_dir: outDirEl.value.trim(),
        resume: document.getElementById("resume").checked,
        links_only: document.getElementById("links_only").checked,
        no_keepalive: document.getElementById("no_keepalive").checked,
        single_page: document.getElementById("single_page").checked,
        user_agent: document.getElementById("user_agent").value.trim(),
      };

      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        showAlert(data.error || "Failed to start job");
        btn.disabled = false;
        return;
      }

      activeJobId = data.id;
      document.getElementById("telemetry").style.display = "block";
      document.getElementById("clock-pill").textContent = "LIVE";
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollJob, 600);
      pollJob();
    });

    async function pollJob() {
      if (!activeJobId) return;
      const res = await fetch(`/api/jobs/${activeJobId}`);
      if (!res.ok) return;
      const j = await res.json();

      document.getElementById("stage-badge").textContent = j.stage || j.status.toUpperCase();
      document.getElementById("elapsed-label").textContent = j.elapsed || "00:00";
      document.getElementById("thread-line").textContent = j.thread_title
        ? `Thread: ${j.thread_title} -> ${j.out_dir}`
        : "Scanning thread...";

      let ratio = 0;
      if (j.stage === "DOWNLOAD" && j.dl_total > 0) {
        ratio = j.dl_done / j.dl_total;
      } else if (j.candidates_total > 0) {
        ratio = (j.candidates_done / j.candidates_total) * (j.dl_total > 0 ? 0.5 : 1.0);
      }
      if (j.status === "completed") ratio = 1;

      const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
      document.getElementById("pct-label").textContent = `${pct}%`;
      document.getElementById("bar-fill").style.width = `${pct}%`;

      document.getElementById("st-resolved").textContent = j.resolved_total ?? 0;
      document.getElementById("st-dl").textContent = j.downloaded ?? 0;
      document.getElementById("st-skip").textContent = j.skipped ?? 0;
      document.getElementById("st-err").textContent = j.errors ?? 0;

      const con = document.getElementById("console");
      con.innerHTML = (j.logs || []).map(row =>
        `<div class="log-row"><span class="log-t">${escHtml(row.time)}</span><span class="log-tag ${escHtml(row.tag)}">[${escHtml(row.tag)}]</span><span>${escHtml(row.msg)}</span></div>`
      ).join("");
      con.scrollTop = con.scrollHeight;

      if (j.status === "completed" || j.status === "failed") {
        clearInterval(pollTimer);
        document.getElementById("submit-btn").disabled = false;
        document.getElementById("clock-pill").textContent = j.status === "completed" ? "DONE" : "ERR";
        loadVault();
      }
    }

    loadVault();
  </script>
</body>
</html>
"""


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/")
def index():
    return render_template_string(HTML_PAGE)


@app.get("/api/files")
def list_files():
    root_dir = _resolve_root_dir(request.args.get("root"))
    return jsonify(_collect_vault_tree(root_dir))


@app.get("/api/files/download/<path:relpath>")
def download_vault_file(relpath):
    root_dir = _resolve_root_dir(request.args.get("root"))
    target = _safe_child_path(root_dir, relpath)
    if not target or not os.path.isfile(target) or target.endswith(".part"):
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


@app.get("/api/files/zip/<path:folder_rel>")
def download_folder_zip(folder_rel):
    root_dir = _resolve_root_dir(request.args.get("root"))
    target_dir = _safe_child_path(root_dir, folder_rel)
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Folder not found"}), 404

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(target_dir):
            for fname in sorted(filenames):
                if fname.endswith(".part") or fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                if not os.path.isfile(fpath):
                    continue
                arcname = os.path.relpath(fpath, target_dir).replace("\\", "/")
                zf.write(fpath, arcname)
                added += 1

    if added == 0:
        return jsonify({"error": "Folder has no completed files"}), 404

    buf.seek(0)
    safe_zip_name = xfdl.sanitize(os.path.basename(target_dir), "archive") + ".zip"
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=safe_zip_name,
    )


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    cookie = (data.get("cookie") or "").strip()
    if not cookie:
        return jsonify({"error": "Cookie is required before running a job."}), 400
    if not url:
        return jsonify({"error": "URL is required."}), 400
    job = _create_job(data)
    return jsonify({"id": job["id"], "status": job["status"]}), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({k: v for k, v in job.items() if k != "created_at"})


def main():
    parser = argparse.ArgumentParser(description="Mobile web UI for XFDL")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
