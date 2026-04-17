"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from scanner import get_db

DB_PATH = Path.home() / ".claude" / "usage.db"

# バックグラウンド自動スキャンの既定間隔（秒）。
# SCAN_INTERVAL_SEC=0 で無効化、環境変数で上書き可能。
DEFAULT_SCAN_INTERVAL_SEC = 300

# /api/data 用の軽量キャッシュ: TTL + DB mtime でキー管理。
# 連続アクセス時の重い SQL 集約をスキップ。
_cache_key = None
_cache_data = None


def _invalidate_cache():
    """定期スキャン後などに呼び出してキャッシュを破棄。"""
    global _cache_key, _cache_data
    _cache_key = None
    _cache_data = None


def get_dashboard_data(db_path=DB_PATH):
    global _cache_key, _cache_data
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    # WAL モードではスキャンの書き込みは usage.db-wal に入り、次のチェックポイントまで
    # 本体 DB の mtime は変わらない。両方を鍵に含めてステイル応答を防ぐ。
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        db_mtime = 0.0
    try:
        wal_mtime = db_path.with_name(db_path.name + "-wal").stat().st_mtime
    except OSError:
        wal_mtime = 0.0
    cache_key = (str(db_path), db_mtime, wal_mtime)
    if _cache_data is not None and _cache_key == cache_key:
        return _cache_data

    conn = get_db(db_path)

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "project":       r["project_name"] or "unknown",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    result = {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache_data = result
    _cache_key = cache_key
    return result


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code 使用状況ダッシュボード</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+JP:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    /* 和紙 (washi) 風パレット */
    --bg:            #f7f4ec;
    --paper:         #ffffff;
    --paper-soft:    #fbfaf4;
    --ink:           #1a1a1a;
    --ink-soft:      #2f2f2f;
    --ink-faint:     #4a4a4a;
    --muted:         #8a8580;
    --muted-soft:    #b0aba3;
    --hairline:      #e5dfd3;
    --hairline-soft: #efeae0;
    --ai:            #1e3a5f;   /* 藍 - indigo */
    --ai-soft:       #4a6b8a;
    --ai-tint:       rgba(30,58,95,0.07);
    --shu:           #b8423c;   /* 朱 - vermillion */
    --kin:           #c9a961;   /* 金 - gold */
    --matsuba:       #2d6a4f;   /* 松葉 - pine */
    --sumire:        #6b5b95;   /* 菫 - violet */
    --ebi:           #8b5a6b;   /* 葡萄 - mauve */
    --kuchiba:       #a07a5a;   /* 朽葉 - kuchiba */

    /* Chart series (light) */
    --chart-input:    rgba(30,58,95,0.85);
    --chart-output:   rgba(107,91,149,0.80);
    --chart-cache-r:  rgba(45,106,79,0.72);
    --chart-cache-c:  rgba(201,169,97,0.80);

    --font-mincho: 'IBM Plex Sans JP', 'Hiragino Sans', 'Yu Gothic UI', sans-serif;
    --font-body:   'IBM Plex Sans JP', 'Hiragino Sans', 'Yu Gothic UI', sans-serif;
    --font-mono:   'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;

    --radius: 3px;
    --shadow-sm: 0 1px 0 rgba(30,40,60,0.03);
    --shadow-md: 0 1px 2px rgba(30,40,60,0.04), 0 12px 32px rgba(30,40,60,0.04);

    --body-gradient:
      radial-gradient(ellipse 900px 600px at 8% 0%, rgba(30,58,95,0.04), transparent 60%),
      radial-gradient(ellipse 700px 500px at 95% 100%, rgba(184,66,60,0.028), transparent 65%);
  }

  /* ── Dark theme (sumi 墨) — auto-detected via prefers-color-scheme ─────── */
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:            #0e0e11;
      --paper:         #17171c;
      --paper-soft:    #1c1c22;
      --ink:           #ebe6d7;    /* 生成り off-white */
      --ink-soft:      #c9c3b4;
      --ink-faint:     #9e9889;
      --muted:         #7a7569;
      --muted-soft:    #4e4a43;
      --hairline:      #2a2a32;
      --hairline-soft: #1f1f26;
      --ai:            #8aa8c8;    /* 浅葱 asagi */
      --ai-soft:       #a3bcd4;
      --ai-tint:       rgba(138,168,200,0.10);
      --shu:           #e07970;
      --kin:           #d8bf82;
      --matsuba:       #77be96;
      --sumire:        #a194cc;
      --ebi:           #c89bab;
      --kuchiba:       #c4a07a;

      --chart-input:    rgba(138,168,200,0.85);
      --chart-output:   rgba(161,148,204,0.80);
      --chart-cache-r:  rgba(119,190,150,0.72);
      --chart-cache-c:  rgba(216,191,130,0.85);

      --shadow-sm: 0 1px 0 rgba(0,0,0,0.25);
      --shadow-md: 0 1px 2px rgba(0,0,0,0.35), 0 12px 32px rgba(0,0,0,0.3);

      --body-gradient:
        radial-gradient(ellipse 900px 600px at 8% 0%, rgba(138,168,200,0.06), transparent 60%),
        radial-gradient(ellipse 700px 500px at 95% 100%, rgba(224,121,112,0.035), transparent 65%);
    }
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { background: var(--bg); }
  body {
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 14px;
    line-height: 1.6;
    font-feature-settings: "palt" 1;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    background: var(--body-gradient), var(--bg);
    min-height: 100vh;
  }

  /* ── Header ────────────────────────────────────────────────────────────── */
  header {
    background: var(--paper);
    border-bottom: 1px solid var(--hairline);
    padding: 30px 48px 24px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    position: relative;
  }
  header::before {
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg,
      var(--ai) 0%, var(--ai-soft) 30%,
      var(--sumire) 50%, var(--shu) 72%, var(--kin) 100%);
  }
  .brand { display: flex; align-items: stretch; gap: 18px; }
  .brand-mark {
    width: 3px;
    background: var(--ai);
    align-self: stretch;
  }
  .brand-title {
    font-family: var(--font-mincho);
    font-size: 26px;
    font-weight: 600;
    color: var(--ink);
    letter-spacing: 0.04em;
    line-height: 1.1;
  }
  .brand-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.22em;
    color: var(--muted);
    text-transform: uppercase;
    margin-top: 8px;
  }
  .header-right { text-align: right; }
  .header-right .meta {
    color: var(--muted);
    font-size: 11px;
    letter-spacing: 0.06em;
    margin-bottom: 10px;
    font-family: var(--font-mono);
  }
  #rescan-btn {
    background: var(--paper);
    border: 1px solid var(--hairline);
    color: var(--ink-faint);
    padding: 7px 18px;
    border-radius: var(--radius);
    cursor: pointer;
    font-size: 12px;
    font-family: var(--font-body);
    letter-spacing: 0.08em;
    transition: all 0.22s ease;
  }
  #rescan-btn:hover {
    background: var(--ai);
    color: var(--paper);
    border-color: var(--ai);
  }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ── Filter bar ────────────────────────────────────────────────────────── */
  #filter-bar {
    background: var(--paper-soft);
    border-bottom: 1px solid var(--hairline);
    padding: 18px 48px;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .filter-label {
    font-family: var(--font-mincho);
    font-size: 13px;
    font-weight: 500;
    color: var(--ai);
    letter-spacing: 0.18em;
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .filter-label::after {
    content: "";
    display: inline-block;
    width: 16px;
    height: 1px;
    background: var(--ai);
  }
  .filter-sep {
    width: 1px;
    height: 22px;
    background: var(--hairline);
    margin: 0 4px;
  }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 5px 13px;
    border-radius: var(--radius);
    border: 1px solid var(--hairline);
    background: var(--paper);
    cursor: pointer;
    font-size: 11.5px;
    color: var(--ink-faint);
    font-family: var(--font-mono);
    letter-spacing: 0.01em;
    transition: all 0.18s ease;
    user-select: none;
  }
  .model-cb-label:hover {
    border-color: var(--ai-soft);
    color: var(--ai);
  }
  .model-cb-label.checked {
    background: var(--ai);
    border-color: var(--ai);
    color: var(--paper);
  }
  .model-cb-label input { display: none; }
  .filter-btn {
    padding: 5px 13px;
    border-radius: var(--radius);
    border: 1px solid var(--hairline);
    background: var(--paper);
    color: var(--ink-faint);
    font-size: 12px;
    font-family: var(--font-body);
    letter-spacing: 0.08em;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.18s ease;
  }
  .filter-btn:hover {
    border-color: var(--ai-soft);
    color: var(--ai);
  }
  .range-group {
    display: flex;
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    overflow: hidden;
    background: var(--paper);
  }
  .range-btn {
    padding: 5px 18px;
    background: transparent;
    border: none;
    border-right: 1px solid var(--hairline);
    color: var(--ink-faint);
    font-size: 12px;
    font-family: var(--font-body);
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.18s ease;
  }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: var(--ai-tint); color: var(--ai); }
  .range-btn.active {
    background: var(--ai);
    color: var(--paper);
    font-weight: 500;
  }

  /* ── Container ─────────────────────────────────────────────────────────── */
  .container {
    max-width: 1440px;
    margin: 0 auto;
    padding: 48px 48px 32px;
  }

  /* ── Stats row ─────────────────────────────────────────────────────────── */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1px;
    margin-bottom: 40px;
    background: var(--hairline);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: var(--shadow-md);
  }
  .stat-card {
    background: var(--paper);
    padding: 24px 22px 22px;
    position: relative;
    transition: background 0.2s;
  }
  .stat-card:hover { background: var(--paper-soft); }
  .stat-card::before {
    content: "";
    position: absolute;
    top: 0; left: 0;
    width: 32px;
    height: 2px;
    background: var(--ai);
  }
  .stat-card.accent-shu::before     { background: var(--shu); }
  .stat-card.accent-matsuba::before { background: var(--matsuba); }
  .stat-card.accent-kin::before     { background: var(--kin); }
  .stat-card.accent-sumire::before  { background: var(--sumire); }
  .stat-card.accent-ebi::before     { background: var(--ebi); }
  .stat-card .label {
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 0.16em;
    margin-bottom: 12px;
    font-weight: 500;
    font-family: var(--font-body);
  }
  .stat-card .value {
    font-family: var(--font-mincho);
    font-size: 28px;
    font-weight: 600;
    color: var(--ink);
    line-height: 1.05;
    letter-spacing: 0.01em;
    font-feature-settings: "tnum" 1;
  }
  .stat-card.accent-matsuba .value { color: var(--matsuba); }
  .stat-card .sub {
    color: var(--muted);
    font-size: 10.5px;
    margin-top: 8px;
    letter-spacing: 0.03em;
  }

  /* ── Chart grid ────────────────────────────────────────────────────────── */
  .charts-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-bottom: 40px;
  }
  .chart-card {
    background: var(--paper);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 28px 28px 24px;
    box-shadow: var(--shadow-sm);
  }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 {
    font-family: var(--font-mincho);
    font-size: 15px;
    font-weight: 500;
    color: var(--ink);
    letter-spacing: 0.06em;
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .chart-card h2::before {
    content: "";
    width: 22px; height: 1px;
    background: var(--ai);
    display: inline-block;
    align-self: center;
  }
  .chart-card h2 .en {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--muted);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    font-weight: 400;
  }
  .chart-wrap { position: relative; height: 260px; margin-top: 20px; }
  .chart-wrap.tall { height: 320px; }

  /* ── Table card ────────────────────────────────────────────────────────── */
  .table-card {
    background: var(--paper);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    padding: 28px 32px 22px;
    margin-bottom: 28px;
    box-shadow: var(--shadow-sm);
    overflow-x: auto;
  }
  .sect-heading {
    font-family: var(--font-mincho);
    font-size: 15px;
    font-weight: 500;
    color: var(--ink);
    letter-spacing: 0.06em;
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 18px;
  }
  .sect-heading::before {
    content: "";
    width: 22px;
    height: 1px;
    background: var(--ai);
    display: inline-block;
    align-self: center;
  }
  .sect-heading .en {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--muted);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    font-weight: 400;
  }
  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }
  .section-header .sect-heading { margin-bottom: 0; }
  .export-btn {
    background: var(--paper);
    border: 1px solid var(--hairline);
    color: var(--ink-faint);
    padding: 5px 14px;
    border-radius: var(--radius);
    cursor: pointer;
    font-size: 11px;
    font-family: var(--font-body);
    letter-spacing: 0.1em;
    transition: all 0.18s ease;
  }
  .export-btn:hover {
    border-color: var(--ai);
    color: var(--ai);
  }

  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left;
    padding: 14px 14px 12px;
    font-size: 10px;
    letter-spacing: 0.14em;
    color: var(--muted);
    border-bottom: 1px solid var(--hairline);
    font-weight: 500;
    font-family: var(--font-body);
    white-space: nowrap;
    text-transform: uppercase;
  }
  th.sortable { cursor: pointer; user-select: none; transition: color 0.15s; }
  th.sortable:hover { color: var(--ai); }
  .sort-icon { font-size: 9px; color: var(--ai); margin-left: 4px; }
  td {
    padding: 13px 14px;
    border-bottom: 1px solid var(--hairline-soft);
    font-size: 13px;
    color: var(--ink-soft);
    font-family: var(--font-body);
  }
  tr:last-child td { border-bottom: none; }
  tbody tr { transition: background 0.15s; }
  tbody tr:hover td { background: rgba(30,58,95,0.025); }
  .model-tag {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 2px;
    font-size: 11px;
    background: var(--ai-tint);
    color: var(--ai);
    font-family: var(--font-mono);
    letter-spacing: 0.01em;
    border: 1px solid rgba(30,58,95,0.14);
  }
  .cost {
    color: var(--matsuba);
    font-family: var(--font-mono);
    font-weight: 500;
    font-feature-settings: "tnum" 1;
  }
  .cost-na {
    color: var(--muted-soft);
    font-family: var(--font-mono);
    font-size: 11px;
  }
  .num {
    font-family: var(--font-mono);
    font-feature-settings: "tnum" 1;
  }
  .muted { color: var(--muted); }
  .session-id {
    font-family: var(--font-mono);
    color: var(--muted);
    font-size: 12px;
    letter-spacing: 0.02em;
  }

  /* ── Footer ────────────────────────────────────────────────────────────── */
  footer {
    border-top: 1px solid var(--hairline);
    background: var(--paper);
    padding: 30px 48px;
    margin-top: 32px;
  }
  .footer-content { max-width: 1440px; margin: 0 auto; }
  .footer-content p {
    color: var(--muted);
    font-size: 11.5px;
    line-height: 1.9;
    letter-spacing: 0.03em;
    margin-bottom: 6px;
  }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a {
    color: var(--ai);
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color 0.18s;
  }
  .footer-content a:hover { border-bottom-color: var(--ai); }
  .footer-content em {
    font-style: normal;
    color: var(--shu);
    font-family: var(--font-mincho);
    font-weight: 500;
    padding: 0 1px;
  }

  @media (max-width: 900px) {
    header { padding: 24px; flex-direction: column; align-items: flex-start; gap: 16px; }
    .header-right { text-align: left; }
    #filter-bar, .container, footer { padding-left: 24px; padding-right: 24px; }
    .charts-grid { grid-template-columns: 1fr; }
    .chart-card.wide { grid-column: 1; }
    .brand-title { font-size: 22px; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark"></div>
    <div>
      <div class="brand-title">Claude Code 使用状況</div>
      <div class="brand-sub">Claude&nbsp;Code&nbsp;&middot;&nbsp;Usage&nbsp;Dashboard</div>
    </div>
  </div>
  <div class="header-right">
    <div class="meta" id="meta">読み込み中…</div>
    <button id="rescan-btn" onclick="triggerRescan()" title="JSONL ファイルを再スキャンしてデータベースを作り直します。データが古い、またはコストが合わないときに使用してください。">&#x21bb; 再スキャン</button>
  </div>
</header>

<div id="filter-bar">
  <div class="filter-label">モデル</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">すべて</button>
  <button class="filter-btn" onclick="clearAllModels()">クリア</button>
  <div class="filter-sep"></div>
  <div class="filter-label">期間</div>
  <div class="range-group">
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7日</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30日</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90日</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">全期間</button>
  </div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>

  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">日次トークン使用量 <span class="en">Daily Token Usage</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>モデル別構成比 <span class="en">By Model</span></h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>上位プロジェクト <span class="en">Top Projects</span></h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <div class="sect-heading">モデル別コスト <span class="en">Cost by Model</span></div>
    <table>
      <thead><tr>
        <th>モデル</th>
        <th class="sortable" onclick="setModelSort('turns')">ターン数 <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">入力 <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">出力 <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">キャッシュ読取 <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">キャッシュ書込 <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">推定コスト <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>

  <div class="table-card">
    <div class="section-header">
      <div class="sect-heading">最近のセッション <span class="en">Recent Sessions</span></div>
      <button class="export-btn" onclick="exportSessionsCSV()" title="フィルタ済みの全セッションを CSV に書き出します">&#x2913; CSV</button>
    </div>
    <table>
      <thead><tr>
        <th>セッション</th>
        <th>プロジェクト</th>
        <th class="sortable" onclick="setSessionSort('last')">最終更新 <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">所要時間 <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>モデル</th>
        <th class="sortable" onclick="setSessionSort('turns')">ターン数 <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">入力 <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">出力 <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">推定コスト <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>

  <div class="table-card">
    <div class="section-header">
      <div class="sect-heading">プロジェクト別コスト <span class="en">Cost by Project</span></div>
      <button class="export-btn" onclick="exportProjectsCSV()" title="全プロジェクトを CSV に書き出します">&#x2913; CSV</button>
    </div>
    <table>
      <thead><tr>
        <th>プロジェクト</th>
        <th class="sortable" onclick="setProjectSort('sessions')">セッション <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">ターン数 <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">入力 <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">出力 <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">推定コスト <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>コスト推定は <a href="https://claude.com/pricing#api" target="_blank">Anthropic API の料金表</a>（2026年4月時点）に基づきます。モデル名に <em>opus</em>・<em>sonnet</em>・<em>haiku</em> を含むもののみコスト計算の対象です。Max / Pro プランの実コストは API 料金と異なります。</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      制作: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      ライセンス: MIT
    </p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};
let lastFilteredSessions = [];
let lastByProject = [];

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-6'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors (和モダンパレット) ────────────────────────────────────────
// Chart colors are read from CSS variables at render time so that
// prefers-color-scheme changes flow through without a page reload.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function getChartColors() {
  return {
    tick:  cssVar('--muted'),
    grid:  cssVar('--hairline-soft'),
    paper: cssVar('--paper'),
    tokens: {
      input:          cssVar('--chart-input'),
      output:         cssVar('--chart-output'),
      cache_read:     cssVar('--chart-cache-r'),
      cache_creation: cssVar('--chart-cache-c'),
    },
    models: [
      cssVar('--ai'),       // 藍
      cssVar('--shu'),      // 朱
      cssVar('--matsuba'),  // 松葉
      cssVar('--kin'),      // 金
      cssVar('--sumire'),   // 菫
      cssVar('--ebi'),      // 葡萄
      cssVar('--ai-soft'),  // 薄藍
      cssVar('--kuchiba'),  // 朽葉
    ],
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { '7d': '直近7日', '30d': '直近30日', '90d': '直近90日', 'all': '全期間' };
const RANGE_TICKS  = { '7d': 7, '30d': 15, '90d': 13, 'all': 12 };

function getRangeCutoff(range) {
  if (range === 'all') return null;
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Generic sort controller ────────────────────────────────────────────────
// 3 組の setXSort/updateXSortIcons/sortX を統合
function createSortCtrl(prefix, defaultCol, defaultDir) {
  let col = defaultCol, dir = defaultDir;
  return {
    get col() { return col; },
    get dir() { return dir; },
    set(newCol) {
      if (col === newCol) { dir = dir === 'desc' ? 'asc' : 'desc'; }
      else { col = newCol; dir = 'desc'; }
      this.updateIcons();
      applyFilter();
    },
    updateIcons() {
      document.querySelectorAll(`[id^="${prefix}"]`).forEach(el => el.textContent = '');
      const icon = document.getElementById(prefix + col);
      if (icon) icon.textContent = dir === 'desc' ? ' \u25bc' : ' \u25b2';
    },
    sort(arr, valFn) {
      return [...arr].sort((a, b) => {
        const av = valFn ? valFn(a, col) : (a[col] ?? 0);
        const bv = valFn ? valFn(b, col) : (b[col] ?? 0);
        if (av < bv) return dir === 'desc' ? 1 : -1;
        if (av > bv) return dir === 'desc' ? -1 : 1;
        return 0;
      });
    }
  };
}

const sessionSortCtrl = createSortCtrl('sort-icon-', 'last', 'desc');
const modelSortCtrl   = createSortCtrl('msort-', 'cost', 'desc');
const projectSortCtrl = createSortCtrl('psort-', 'cost', 'desc');

// onclick ハンドラ（HTML から呼び出し）
function setSessionSort(col) { sessionSortCtrl.set(col); }
function setModelSort(col)   { modelSortCtrl.set(col); }
function setProjectSort(col) { projectSortCtrl.set(col); }


function sessionValFn(item, col) {
  if (col === 'cost') return calcCost(item.model, item.input, item.output, item.cache_read, item.cache_creation);
  if (col === 'duration_min') return parseFloat(item.duration_min) || 0;
  return item[col] ?? 0;
}
function sortSessions(sessions) { return sessionSortCtrl.sort(sessions, sessionValFn); }

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const cutoff = getRangeCutoff(selectedRange);

  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );

  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!cutoff || s.last_date >= cutoff)
  );

  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };

  document.getElementById('daily-chart-title').innerHTML =
    '日次トークン使用量 <span class="en">' + esc(RANGE_LABELS[selectedRange]) + '</span>';

  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByProject = sortProjects(byProject);
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
  renderProjectCostTable(lastByProject.slice(0, 20));
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange];
  const stats = [
    { label: 'セッション数',     value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'ターン数',         value: fmt(t.turns),                sub: rangeLabel },
    { label: '入力トークン',     value: fmt(t.input),                sub: rangeLabel },
    { label: '出力トークン',     value: fmt(t.output),               sub: rangeLabel, cls: 'accent-sumire' },
    { label: 'キャッシュ読取',   value: fmt(t.cache_read),           sub: 'プロンプトキャッシュから', cls: 'accent-matsuba' },
    { label: 'キャッシュ書込',   value: fmt(t.cache_creation),       sub: 'キャッシュへの書き込み',   cls: 'accent-kin' },
    { label: '推定コスト',       value: fmtCostBig(t.cost),          sub: 'API料金・2026年4月',       cls: 'accent-matsuba' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card ${s.cls || ''}">
      <div class="label">${esc(s.label)}</div>
      <div class="value">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  const colors = getChartColors();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: '入力',         data: daily.map(d => d.input),          backgroundColor: colors.tokens.input,          stack: 'tokens' },
        { label: '出力',         data: daily.map(d => d.output),         backgroundColor: colors.tokens.output,         stack: 'tokens' },
        { label: 'キャッシュ読取', data: daily.map(d => d.cache_read),     backgroundColor: colors.tokens.cache_read,     stack: 'tokens' },
        { label: 'キャッシュ書込', data: daily.map(d => d.cache_creation), backgroundColor: colors.tokens.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: colors.tick, boxWidth: 12, font: { family: "'IBM Plex Sans JP', sans-serif", size: 11 } } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) + ' トークン' } }
      },
      scales: {
        x: { ticks: { color: colors.tick, maxTicksLimit: RANGE_TICKS[selectedRange], font: { family: "'JetBrains Mono', monospace", size: 10 } }, grid: { color: colors.grid } },
        y: { ticks: { color: colors.tick, callback: v => fmt(v), font: { family: "'JetBrains Mono', monospace", size: 10 } }, grid: { color: colors.grid } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  const colors = getChartColors();
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{
        data: byModel.map(m => m.input + m.output),
        backgroundColor: colors.models,
        borderWidth: 2,
        borderColor: colors.paper
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: '62%',
      plugins: {
        legend: { position: 'bottom', labels: { color: colors.tick, boxWidth: 12, font: { family: "'JetBrains Mono', monospace", size: 10 } } },
        tooltip: { callbacks: { label: c => ' ' + c.label + ': ' + fmt(c.raw) + ' トークン' } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  const colors = getChartColors();
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: '入力', data: top.map(p => p.input),  backgroundColor: colors.tokens.input },
        { label: '出力', data: top.map(p => p.output), backgroundColor: colors.tokens.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: colors.tick, boxWidth: 12, font: { family: "'IBM Plex Sans JP', sans-serif", size: 11 } } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + fmt(c.raw) + ' トークン' } }
      },
      scales: {
        x: { ticks: { color: colors.tick, callback: v => fmt(v), font: { family: "'JetBrains Mono', monospace", size: 10 } }, grid: { color: colors.grid } },
        y: { ticks: { color: colors.tick, font: { family: "'JetBrains Mono', monospace", size: 10 } }, grid: { color: colors.grid } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="num cost">${fmtCost(cost)}</td>`
      : `<td class="num cost-na">&mdash;</td>`;
    return `<tr>
      <td class="session-id">${esc(s.session_id)}&hellip;</td>
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="num muted">${esc(s.duration_min)} 分</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function modelValFn(item, col) {
  if (col === 'cost') return calcCost(item.model, item.input, item.output, item.cache_read, item.cache_creation);
  return item[col] ?? 0;
}
function sortModels(byModel) { return modelSortCtrl.sort(byModel, modelValFn); }

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = sortModels(byModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="num cost">${fmtCost(cost)}</td>`
      : `<td class="num cost-na">&mdash;</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function sortProjects(byProject) { return projectSortCtrl.sort(byProject); }

function renderProjectCostTable(byProject) {
  document.getElementById('project-cost-body').innerHTML = byProject.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="num cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  // Excel の日本語文字化け対策に BOM を付与
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportSessionsCSV() {
  const header = ['セッション', 'プロジェクト', '最終更新', '所要時間(分)', 'モデル', 'ターン数', '入力', '出力', 'キャッシュ読取', 'キャッシュ書込', '推定コスト'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['プロジェクト', 'セッション', 'ターン数', '入力', '出力', 'キャッシュ読取', 'キャッシュ書込', '推定コスト'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb スキャン中…';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb 完了 (新規 ' + d.new + ' / 更新 ' + d.updated + ')';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb エラー';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb 再スキャン'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:48px;color:#b8423c;font-family:\'IBM Plex Sans JP\',sans-serif;font-size:16px">' + esc(d.error) + '</div>';
      return;
    }
    document.getElementById('meta').textContent = '更新 ' + d.generated_at + ' \u00b7 30 秒後に自動更新';

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      buildFilterUI(d.all_models);
      sessionSortCtrl.updateIcons();
      modelSortCtrl.updateIcons();
      projectSortCtrl.updateIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

loadData();
setInterval(loadData, 30000);

// OS のテーマ切り替えを検知してチャートを再描画（HTML/CSS は @media で自動追従）
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (rawData !== null) applyFilter();
  });
}
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # `self.path` はクエリ・フラグメント込み。`/?range=7d` のような
        # リロードで 404 にならないよう、urlparse でパス部分のみ抽出して比較する。
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/data":
            try:
                self._send_json(get_dashboard_data())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/rescan":
            if DB_PATH.exists():
                DB_PATH.unlink()
            from scanner import scan
            result = scan(verbose=False)
            _invalidate_cache()
            self._send_json(result)
        else:
            self.send_response(404)
            self.end_headers()


def _periodic_scan_loop(interval_sec, projects_dir=None):
    """バックグラウンドで定期的に scan を走らせ、キャッシュを破棄する。

    PC 起動時しかスキャンしていなかったため、日をまたぐと当日の統計が
    反映されなかった。ダッシュボード稼働中も自動で最新 JSONL を取り込む。

    projects_dir: CLI で ``--projects-dir`` が指定された場合、初回 scan と
    同じディレクトリを周期スキャンでも使う。None ならスキャナー既定値
    (``DEFAULT_PROJECTS_DIRS``) を利用。
    """
    # scanner のインポートはスレッド内で行って起動時間を早める
    from scanner import scan

    while True:
        try:
            time.sleep(interval_sec)
            scan(projects_dir=projects_dir, verbose=False)
            _invalidate_cache()
        except Exception as exc:
            # サーバーは落とさず、次の周期で再挑戦する
            print(f"[periodic scan error] {exc}")


def _resolve_scan_interval(scan_interval_sec):
    """SCAN_INTERVAL_SEC のパースを失敗に強くする。

    `"300s"` のような不正値でもサーバーを落とさず、デフォルト値に
    フォールバックする。
    """
    if scan_interval_sec is not None:
        return scan_interval_sec
    raw = os.environ.get("SCAN_INTERVAL_SEC", str(DEFAULT_SCAN_INTERVAL_SEC))
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[warn] invalid SCAN_INTERVAL_SEC={raw!r}; "
              f"falling back to {DEFAULT_SCAN_INTERVAL_SEC}s")
        return DEFAULT_SCAN_INTERVAL_SEC


def serve(host=None, port=None, scan_interval_sec=None, projects_dir=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))

    scan_interval_sec = _resolve_scan_interval(scan_interval_sec)

    if scan_interval_sec > 0:
        t = threading.Thread(
            target=_periodic_scan_loop,
            args=(scan_interval_sec,),
            kwargs={"projects_dir": projects_dir},
            daemon=True,
            name="claude-usage-periodic-scan",
        )
        t.start()
        print(f"Periodic scan enabled: every {scan_interval_sec}s")
    else:
        print("Periodic scan disabled (SCAN_INTERVAL_SEC=0)")

    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
