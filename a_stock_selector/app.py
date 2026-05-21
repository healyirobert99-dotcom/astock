from __future__ import annotations

from html import escape

import pandas as pd

from .config import DEFAULT_DB_PATH, RISK_WARNING
from .data_provider import SampleDataProvider, TushareDataProvider, save_dataset, save_dataset_incremental
from .db import connect, init_db, read_sql, table_count
from .strategy import run_strategy


CANDIDATE_EXPORT_COLUMNS = [
    "code",
    "name",
    "industry",
    "status",
    "candidate_layer",
    "mainline_status",
    "mainline_base_score",
    "signal_status",
    "market_score",
    "industry_score",
    "fundamental_status",
    "trend_template_type",
    "keypoint_type",
    "keypoint_price",
    "keypoint_distance_pct",
    "volume_quality",
    "close_quality",
    "risk_level",
    "trade_plan_type",
    "suggested_action",
    "watch_price",
    "trigger_price",
    "buy_lower",
    "buy_upper",
    "suggested_buy_price",
    "stop_loss_price",
    "take_profit_1",
    "take_profit_2",
    "trailing_stop_rule",
    "suggested_position",
    "include_reason",
    "exclude_reason",
    "rejected_reason_detail",
    "risk_warning",
]


FUNNEL_LABELS = {
    "total_stock_count": ("全市场股票", "原始扫描股票数量"),
    "after_basic_filter_count": ("基础过滤后", "剔除ST、停牌、低价、低流动性"),
    "after_fundamental_filter_count": ("基本面过滤后", "净利润、扣非、营收、负债率"),
    "after_trend_filter_count": ("趋势模板后", "A/B类趋势模板"),
    "after_keypoint_filter_count": ("关键点突破后", "新高或平台突破且突破质量合格"),
    "after_mainline_filter_count": ("主线确认后", "行业已确认市场主线"),
    "after_market_filter_count": ("市场评分过滤后", "市场评分达到可交易阈值"),
    "candidate_count": ("最终候选", "通过市场、主线、个股全部条件"),
}


RUN_LOG_DISPLAY_COLUMNS = {
    "batch_id": "批次",
    "run_timestamp": "运行时间",
    "trade_date": "交易日",
    "data_source": "数据源",
    "status": "状态",
    "message": "说明",
    "total_stock_count": "全市场",
    "after_basic_filter_count": "基础过滤后",
    "after_fundamental_filter_count": "基本面过滤后",
    "after_trend_filter_count": "趋势模板后",
    "after_keypoint_filter_count": "关键点突破后",
    "after_mainline_filter_count": "主线确认后",
    "after_market_filter_count": "市场评分过滤后",
    "candidate_count": "候选",
    "excluded_count": "剔除",
}


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="a_stock_selector", page_icon="AS", layout="wide")
    init_db(DEFAULT_DB_PATH)
    inject_theme(st)

    with connect(DEFAULT_DB_PATH) as conn:
        snapshot = current_snapshot(conn)
        latest_log = latest_run_log(conn)
        render_header(st, snapshot, latest_log)

        with st.sidebar:
            # Brand
            st.markdown(
                '<div class="sidebar-brand">'
                '<div class="sidebar-brand-name"><span class="sidebar-brand-dot"></span>Mainline Pivot Scanner</div>'
                '<div class="sidebar-brand-sub">A-Share Post-Market Selector</div>'
                '</div>',
                unsafe_allow_html=True,
            )

            # ── Data Operations ──
            st.markdown('<div class="sidebar-section-label">数据操作</div>', unsafe_allow_html=True)

            snapshot_for_action = current_snapshot(conn)
            can_run_real_strategy = snapshot_for_action is not None and snapshot_for_action["data_source"] == "tushare"

            if st.button("⭳ 刷新 Tushare 全市场", use_container_width=True, type="secondary",
                         help="从Tushare拉取最新行情、财务和基础数据"):
                try:
                    progress = progress_widgets(st, "刷新全市场数据")
                    snapshot_for_refresh = current_snapshot(conn)
                    skip_dates = reusable_trade_dates(conn) if snapshot_for_refresh and snapshot_for_refresh["data_source"] == "tushare" else set()
                    dataset = TushareDataProvider(max_stocks=0, skip_trade_dates=skip_dates, progress_callback=progress).fetch()
                    progress("准备写入 SQLite", 0, 100)
                    if skip_dates:
                        save_dataset_incremental(conn, dataset, progress_callback=progress)
                        mode = "增量"
                    else:
                        save_dataset(conn, dataset, progress_callback=progress)
                        mode = "全量"
                    st.success(f"{mode}刷新完成 · 股票池 {len(dataset.stock_basic)} 只 · 新增日线 {len(dataset.stock_daily)} 条")
                except Exception as exc:
                    st.error(f"Tushare 刷新失败：{exc}")

            col1, col2 = st.columns([3, 1])
            with col1:
                if st.button("▶ 运行全市场策略", type="primary", use_container_width=True, disabled=not can_run_real_strategy):
                    progress = progress_widgets(st, "运行全市场策略")
                    summary = run_strategy(conn, progress_callback=progress)
                    st.session_state["_last_batch"] = summary.batch_id
                    st.success(f"策略完成 batch={summary.batch_id} · 候选 {summary.candidate_count} 只")
            with col2:
                if st.button("🔄", use_container_width=True, help="快速：刷新+运行", disabled=not can_run_real_strategy):
                    progress = progress_widgets(st, "批量刷新并运行")
                    skip_dates = reusable_trade_dates(conn)
                    dataset = TushareDataProvider(max_stocks=0, skip_trade_dates=skip_dates, progress_callback=progress).fetch()
                    progress("准备写入 SQLite", 0, 100)
                    save_dataset_incremental(conn, dataset, progress_callback=progress)
                    progress("数据写入完成，准备运行策略", 100, 100)
                    summary = run_strategy(conn, progress_callback=progress)
                    st.success(f"刷新+策略完成 · 候选 {summary.candidate_count} 只")

            if not can_run_real_strategy:
                st.caption("请先刷新 Tushare 全市场数据")

            st.markdown('<div class="sidebar-section-label">演示数据</div>', unsafe_allow_html=True)
            st.caption("用于功能演示，会清空真实数据。")
            if st.button("初始化样例数据", use_container_width=True, type="secondary"):
                dataset = SampleDataProvider().fetch()
                progress = progress_widgets(st, "写入演示数据")
                save_dataset(conn, dataset, progress_callback=progress)
                st.success("样例数据已写入")

            # ── Navigation ──
            st.markdown('<div class="sidebar-section-label">页面导航</div>', unsafe_allow_html=True)
            page = st.radio(
                "页面",
                ["市场总览", "主线雷达", "个股候选池", "观察池", "策略运行日志"],
                label_visibility="collapsed",
            )

            # ── Quick Status ──
            if can_run_real_strategy and table_count(conn, "run_log") > 0:
                st.markdown('<div class="sidebar-section-label">快速状态</div>', unsafe_allow_html=True)
                recent = conn.execute(
                    "SELECT candidate_count, trade_date FROM run_log ORDER BY run_timestamp DESC LIMIT 1"
                ).fetchone()
                market = conn.execute(
                    "SELECT total_score, risk_level FROM market_score ORDER BY run_timestamp DESC LIMIT 1"
                ).fetchone()
                if recent and market:
                    score = float(market["total_score"])
                    level = str(market["risk_level"])
                    cls = {"积极": "green", "可交易": "teal", "观察": "amber", "谨慎": "red"}.get(level, "muted")
                    st.markdown(
                        f'<div style="font-size:12px;color:var(--text-muted);padding:4px 0;">'
                        f'交易日 <strong style="color:var(--text-primary)">{recent["trade_date"]}</strong><br>'
                        f'市场 <span class="badge badge-{cls}">{level} {score:.0f}分</span><br>'
                        f'候选 <strong style="color:var(--accent)">{recent["candidate_count"]}</strong> 只'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        if table_count(conn, "stock_daily") == 0:
            render_empty_state(st, "还没有行情数据", "请先在左侧刷新 Tushare 数据，或初始化样例数据用于演示。")
            return
        snapshot = current_snapshot(conn)
        if snapshot is None or snapshot["data_source"] != "tushare":
            render_snapshot_bar(st, snapshot) if snapshot is not None else None
            render_empty_state(
                st,
                "尚未加载 Tushare 全市场数据",
                "系统默认只使用 Tushare 作为真实数据源。左侧“演示数据”不会进入默认工作流，请点击“刷新 Tushare 全市场”。",
            )
            return
        if snapshot is not None:
            render_snapshot_bar(st, snapshot)
        if table_count(conn, "run_log") == 0:
            render_empty_state(st, "行情已就绪，策略尚未运行", "点击左侧“运行策略”生成市场评分、行业主线和候选池。")
            return

        if page == "市场总览":
            render_market_overview(st, conn)
        elif page == "主线雷达":
            render_mainline_radar(st, conn)
        elif page == "个股候选池":
            render_candidates(st, conn)
        elif page == "观察池":
            render_watch_pool(st, conn)
        else:
            render_run_log(st, conn)


def inject_theme(st) -> None:
    st.markdown(
        """
        <style>
        /* ═══════════════════════════════════════════
           DESIGN SYSTEM — Dark Terminal Precision
           ═══════════════════════════════════════════ */

        /* ── Design Tokens ─────────────────────── */
        :root {
            /* Core palette */
            --bg-deep: #080d18;
            --bg-base: #0f1923;
            --bg-elevated: #152230;
            --bg-surface: #1a2a3b;
            --bg-hover: #1f3246;
            --bg-input: #0d1520;

            /* Accent — Teal-Cyan */
            --accent: #2dd4bf;
            --accent-dim: #14b8a6;
            --accent-ghost: rgba(45, 212, 191, 0.10);
            --accent-glow: rgba(45, 212, 191, 0.18);

            /* Secondary — Steel Blue */
            --accent2: #60a5fa;
            --accent2-dim: #3b82f6;
            --accent2-ghost: rgba(96, 165, 250, 0.10);

            /* Semantic */
            --good: #10b981;
            --good-ghost: rgba(16, 185, 129, 0.12);
            --warn: #f59e0b;
            --warn-ghost: rgba(245, 158, 11, 0.12);
            --danger: #ef4444;
            --danger-ghost: rgba(239, 68, 68, 0.12);

            /* Text hierarchy */
            --text-primary: #e8edf4;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --text-dim: #475569;

            /* Borders */
            --border-subtle: rgba(148, 163, 184, 0.10);
            --border-default: rgba(148, 163, 184, 0.16);
            --border-strong: rgba(148, 163, 184, 0.24);
            --border-accent: rgba(45, 212, 191, 0.30);

            /* Spacing scale (4px base) */
            --space-1: 0.25rem;
            --space-2: 0.5rem;
            --space-3: 0.75rem;
            --space-4: 1rem;
            --space-5: 1.25rem;
            --space-6: 1.5rem;
            --space-8: 2rem;
            --space-10: 2.5rem;
            --space-12: 3rem;

            /* Radii */
            --radius-sm: 6px;
            --radius-md: 10px;
            --radius-lg: 16px;
            --radius-xl: 22px;
            --radius-full: 9999px;

            /* Shadows */
            --shadow-sm: 0 1px 2px rgba(0,0,0,0.3);
            --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
            --shadow-lg: 0 8px 30px rgba(0,0,0,0.5);
            --shadow-glow: 0 0 20px var(--accent-glow);

            /* Transitions */
            --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
            --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
            --duration-fast: 150ms;
            --duration-normal: 250ms;
            --duration-slow: 400ms;

            /* Typography */
            --font-sans: 'Segoe UI', system-ui, -apple-system, sans-serif;
            --font-mono: 'Cascadia Code', 'JetBrains Mono', 'SF Mono', 'Consolas', monospace;
            --font-display: var(--font-sans);
        }

        /* ── Global Base ───────────────────────── */
        .stApp {
            background:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(45, 212, 191, 0.04), transparent),
                radial-gradient(ellipse 40% 60% at 80% 80%, rgba(96, 165, 250, 0.03), transparent),
                var(--bg-base);
            color: var(--text-primary);
        }
        .stApp * {
            font-family: var(--font-sans);
        }
        header[data-testid="stHeader"] { display: none; }
        div[data-testid="stToolbar"], #MainMenu, footer, .stDeployButton { display: none !important; }

        .block-container {
            padding-top: var(--space-4);
            padding-bottom: var(--space-12);
            max-width: 1440px;
        }

        /* ── Scrollbar ─────────────────────────── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: var(--radius-full); }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

        /* ═══════════════════════════════════════════
           SIDEBAR
           ═══════════════════════════════════════════ */
        section[data-testid="stSidebar"] {
            background: var(--bg-deep);
            border-right: 1px solid var(--border-subtle);
        }
        section[data-testid="stSidebar"] * { color: var(--text-secondary); }
        section[data-testid="stSidebar"] .st-emotion-cache-10trblm { color: var(--text-primary); }

        /* Sidebar branding */
        .sidebar-brand {
            padding: var(--space-3) var(--space-2) var(--space-2);
            margin-bottom: var(--space-1);
        }
        .sidebar-brand-name {
            font-size: 20px;
            font-weight: 720;
            letter-spacing: -0.01em;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: var(--space-2);
        }
        .sidebar-brand-dot {
            width: 8px; height: 8px;
            border-radius: var(--radius-full);
            background: var(--accent);
            box-shadow: 0 0 8px var(--accent-glow);
        }
        .sidebar-brand-sub {
            font-size: 11px;
            color: var(--text-muted);
            margin-top: var(--space-1);
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }

        /* Sidebar sections */
        .sidebar-section-label {
            font-size: 10px;
            font-weight: 650;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--text-dim);
            padding: var(--space-3) var(--space-2) var(--space-1);
            margin-top: var(--space-2);
            border-top: 1px solid var(--border-subtle);
        }

        /* Sidebar buttons */
        div.stButton > button {
            border-radius: var(--radius-md);
            font-weight: 550;
            font-size: 13px;
            letter-spacing: 0.01em;
            border: 1px solid var(--border-default);
            background: var(--bg-elevated);
            color: var(--text-primary);
            padding: var(--space-2) var(--space-4);
            transition: all var(--duration-fast) var(--ease-out);
        }
        div.stButton > button:hover {
            border-color: var(--border-accent);
            background: var(--bg-hover);
            color: var(--accent);
        }
        div.stButton > button[kind="primary"] {
            border-color: var(--accent-dim);
            background: var(--accent-ghost);
            color: var(--accent);
            font-weight: 600;
        }
        div.stButton > button[kind="primary"]:hover {
            background: rgba(45, 212, 191, 0.18);
            box-shadow: var(--shadow-glow);
        }
        div.stButton > button:disabled {
            background: var(--bg-input) !important;
            color: var(--text-dim) !important;
            border-color: var(--border-subtle) !important;
            opacity: 1 !important;
            cursor: not-allowed;
        }

        /* Sidebar radio */
        div[role="radiogroup"] { gap: 2px; }
        div[role="radiogroup"] label {
            padding: var(--space-2) var(--space-3);
            border-radius: var(--radius-md);
            font-size: 13px;
            transition: all var(--duration-fast) var(--ease-out);
            color: var(--text-secondary);
        }
        div[role="radiogroup"] label:hover {
            background: var(--bg-elevated);
            color: var(--text-primary);
        }
        div[role="radiogroup"] label[data-selected="true"] {
            background: var(--accent-ghost);
            color: var(--accent);
            font-weight: 600;
        }

        /* Sidebar expander */
        .streamlit-expanderHeader {
            font-size: 12px;
            color: var(--text-muted);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            padding: var(--space-2) var(--space-3);
            background: var(--bg-elevated);
        }
        .streamlit-expanderHeader:hover { border-color: var(--border-default); }
        .streamlit-expanderContent { border: none; background: transparent; }

        /* ── Sidebar divider ──────────────────── */
        section[data-testid="stSidebar"] hr {
            border-color: var(--border-subtle);
            margin: var(--space-3) 0;
        }

        /* ═══════════════════════════════════════════
           MAIN CONTENT — HEADER
           ═══════════════════════════════════════════ */
        .terminal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: var(--space-6);
            padding: var(--space-3) var(--space-1) var(--space-4);
            margin-bottom: var(--space-3);
            flex-wrap: wrap;
        }
        .terminal-title {
            font-size: 26px;
            font-weight: 720;
            line-height: 1.15;
            letter-spacing: -0.02em;
            color: var(--text-primary);
        }
        .terminal-subtitle {
            color: var(--text-muted);
            font-size: 13px;
            margin-top: var(--space-1);
        }
        .terminal-meta {
            display: grid;
            grid-template-columns: repeat(3, minmax(100px, 1fr));
            gap: var(--space-2);
            min-width: 360px;
        }
        .meta-cell {
            border: 1px solid var(--border-default);
            border-radius: var(--radius-lg);
            padding: var(--space-3) var(--space-4);
            background: var(--bg-elevated);
            transition: border-color var(--duration-fast);
        }
        .meta-cell:hover { border-color: var(--border-strong); }
        .meta-label {
            color: var(--text-dim);
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin-bottom: var(--space-1);
        }
        .meta-value {
            color: var(--text-primary);
            font-size: 18px;
            font-weight: 700;
            font-family: var(--font-mono);
        }

        /* ── Risk Warning Strip ───────────────── */
        .risk-strip {
            border: 1px solid rgba(245, 158, 11, 0.25);
            border-left: 3px solid var(--warn);
            background: var(--warn-ghost);
            color: var(--warn);
            border-radius: var(--radius-md);
            padding: var(--space-3) var(--space-4);
            font-size: 12px;
            margin-bottom: var(--space-6);
            line-height: 1.5;
        }

        /* ── Data Source Snapshot Bar ──────────── */
        .snapshot-bar {
            display: flex;
            flex-wrap: wrap;
            gap: var(--space-2);
            margin: var(--space-2) 0 var(--space-5);
        }
        .status-pill {
            border: 1px solid var(--border-default);
            background: var(--bg-surface);
            border-radius: var(--radius-full);
            padding: var(--space-2) var(--space-4);
            font-size: 11px;
            color: var(--text-muted);
        }
        .status-pill strong {
            color: var(--text-primary);
            margin-left: var(--space-1);
            font-family: var(--font-mono);
        }

        /* ═══════════════════════════════════════════
           SECTION HEADERS
           ═══════════════════════════════════════════ */
        .section-kicker {
            color: var(--accent);
            font-size: 10px;
            font-weight: 650;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: var(--space-1);
        }
        .section-title {
            font-size: 22px;
            font-weight: 700;
            letter-spacing: -0.01em;
            color: var(--text-primary);
            margin-bottom: var(--space-5);
        }

        /* ═══════════════════════════════════════════
           METRIC CARDS (Dashboard Tiles)
           ═══════════════════════════════════════════ */
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: var(--space-3);
            align-items: stretch;
            margin: var(--space-3) 0 var(--space-2);
        }
        .metric-card {
            border: 1px solid var(--border-default);
            background: linear-gradient(180deg, var(--bg-surface) 0%, var(--bg-elevated) 100%);
            border-radius: var(--radius-lg);
            padding: var(--space-4) var(--space-5);
            min-height: 100px;
            min-width: 0;
            transition: border-color var(--duration-fast);
            overflow-wrap: normal;
            word-break: keep-all;
        }
        .metric-card:hover { border-color: var(--border-strong); }
        .metric-label {
            color: var(--text-muted);
            font-size: 11px;
            font-weight: 550;
            letter-spacing: 0.03em;
            margin-bottom: var(--space-2);
            white-space: nowrap;
        }
        .metric-value {
            color: var(--text-primary);
            font-size: 28px;
            font-weight: 750;
            line-height: 1;
            font-family: var(--font-mono);
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .metric-note {
            color: var(--text-dim);
            font-size: 11px;
            margin-top: var(--space-2);
            line-height: 1.35;
            white-space: normal;
        }

        /* ── Market Regime Indicator ───────────── */
        .regime-active .metric-value { color: var(--good); }
        .regime-tradable .metric-value { color: var(--accent); }
        .regime-watch .metric-value { color: var(--warn); }
        .regime-cautious .metric-value { color: var(--danger); }

        /* Regime status badge */
        .regime-badge {
            display: inline-flex;
            align-items: center;
            gap: var(--space-2);
            padding: var(--space-1) var(--space-3);
            border-radius: var(--radius-full);
            font-size: 12px;
            font-weight: 650;
            letter-spacing: 0.02em;
        }
        .regime-badge.active {
            background: var(--good-ghost);
            color: var(--good);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        .regime-badge.tradable {
            background: var(--accent-ghost);
            color: var(--accent);
            border: 1px solid rgba(45, 212, 191, 0.3);
        }
        .regime-badge.watch {
            background: var(--warn-ghost);
            color: var(--warn);
            border: 1px solid rgba(245, 158, 11, 0.3);
        }
        .regime-badge.cautious {
            background: var(--danger-ghost);
            color: var(--danger);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }

        /* ═══════════════════════════════════════════
           PANELS & CARDS
           ═══════════════════════════════════════════ */
        .panel {
            border: 1px solid var(--border-default);
            background: var(--bg-surface);
            border-radius: var(--radius-xl);
            padding: var(--space-5);
            margin-bottom: var(--space-4);
        }
        .panel-title {
            font-size: 14px;
            font-weight: 650;
            color: var(--text-primary);
            margin-bottom: var(--space-4);
            display: flex;
            align-items: center;
            gap: var(--space-2);
        }
        .panel-title::before {
            content: '';
            display: inline-block;
            width: 4px;
            height: 16px;
            border-radius: 2px;
            background: var(--accent);
        }

        /* ═══════════════════════════════════════════
           RANK TABLE (Industry / Mainline List)
           ═══════════════════════════════════════════ */
        .rank-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0 4px;
            font-size: 13px;
            table-layout: auto;
        }
        .rank-table th {
            color: var(--text-dim);
            font-weight: 600;
            text-align: left;
            padding: 0 10px 6px;
            font-size: 10px;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }
        .rank-table td {
            background: var(--bg-elevated);
            border: 1px solid var(--border-subtle);
            padding: 10px 12px;
            vertical-align: middle;
            transition: background var(--duration-fast);
            word-break: keep-all;
            overflow-wrap: normal;
        }
        .rank-table th:nth-child(1), .rank-table td:nth-child(1) { width: 42px; min-width: 42px; }
        .rank-table th:nth-child(2), .rank-table td:nth-child(2) { min-width: 108px; }
        .rank-table th:nth-child(3), .rank-table td:nth-child(3) { min-width: 82px; }
        .rank-table th:nth-child(4), .rank-table td:nth-child(4) { min-width: 58px; }
        .rank-table tr:hover td { background: var(--bg-hover); }
        .rank-table td:first-child { border-radius: var(--radius-md) 0 0 var(--radius-md); border-right: none; }
        .rank-table td:last-child { border-radius: 0 var(--radius-md) var(--radius-md) 0; border-left: none; }
        .rank-index {
            color: var(--text-dim);
            font-size: 12px;
            font-weight: 600;
            font-family: var(--font-mono);
        }
        .rank-name {
            font-weight: 650;
            color: var(--text-primary);
            white-space: nowrap;
            word-break: keep-all;
        }
        .rank-sub {
            color: var(--text-muted);
            font-size: 11px;
            margin-top: 2px;
            line-height: 1.35;
        }
        .rank-value {
            font-weight: 650;
            font-family: var(--font-mono);
            font-variant-numeric: tabular-nums;
        }
        /* Score bar inside table */
        .score-bar {
            width: 100%;
            height: 5px;
            background: rgba(148, 163, 184, 0.10);
            border-radius: var(--radius-full);
            overflow: hidden;
            margin-top: 5px;
        }
        .score-fill {
            height: 100%;
            border-radius: var(--radius-full);
            transition: width 0.6s var(--ease-out);
        }
        .score-fill-hi {
            background: linear-gradient(90deg, #2dd4bf, #14b8a6);
        }
        .score-fill-mid {
            background: linear-gradient(90deg, #60a5fa, #2dd4bf);
        }
        .score-fill-lo {
            background: linear-gradient(90deg, #64748b, #60a5fa);
        }

        /* ═══════════════════════════════════════════
           BADGES
           ═══════════════════════════════════════════ */
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            border-radius: var(--radius-full);
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.02em;
        }
        .badge-teal {
            color: var(--accent);
            background: var(--accent-ghost);
            border: 1px solid rgba(45, 212, 191, 0.25);
        }
        .badge-blue {
            color: var(--accent2);
            background: var(--accent2-ghost);
            border: 1px solid rgba(96, 165, 250, 0.25);
        }
        .badge-green {
            color: var(--good);
            background: var(--good-ghost);
            border: 1px solid rgba(16, 185, 129, 0.25);
        }
        .badge-amber {
            color: var(--warn);
            background: var(--warn-ghost);
            border: 1px solid rgba(245, 158, 11, 0.25);
        }
        .badge-red {
            color: var(--danger);
            background: var(--danger-ghost);
            border: 1px solid rgba(239, 68, 68, 0.25);
        }
        .badge-muted {
            color: var(--text-muted);
            background: transparent;
            border: 1px solid var(--border-default);
        }

        /* Pulse for "live" indicators */
        @keyframes pulse-accent {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .pulse-dot {
            width: 6px; height: 6px;
            border-radius: var(--radius-full);
            background: var(--accent);
            animation: pulse-accent 2s ease-in-out infinite;
            display: inline-block;
        }

        /* ═══════════════════════════════════════════
           EMPTY / ERROR STATES
           ═══════════════════════════════════════════ */
        .empty-panel {
            border: 1px dashed var(--border-strong);
            background: var(--bg-elevated);
            border-radius: var(--radius-lg);
            padding: var(--space-8) var(--space-6);
            color: var(--text-muted);
            text-align: center;
            line-height: 1.6;
        }
        .empty-panel strong {
            display: block;
            color: var(--text-primary);
            font-size: 15px;
            margin-bottom: var(--space-2);
        }

        /* ═══════════════════════════════════════════
           FUNNEL VISUALIZATION
           ═══════════════════════════════════════════ */
        .funnel-container {
            display: flex;
            gap: var(--space-2);
            margin: var(--space-4) 0;
        }
        .funnel-step {
            flex: 1;
            text-align: center;
            padding: var(--space-3) var(--space-2);
            border-radius: var(--radius-md);
            border: 1px solid var(--border-default);
            background: var(--bg-elevated);
        }
        .funnel-count {
            font-family: var(--font-mono);
            font-size: 20px;
            font-weight: 700;
            color: var(--text-primary);
        }
        .funnel-label {
            font-size: 10px;
            color: var(--text-dim);
            margin-top: var(--space-1);
        }
        .funnel-arrow {
            display: flex;
            align-items: center;
            color: var(--text-dim);
            font-size: 18px;
        }

        /* ═══════════════════════════════════════════
           STREAMLIT OVERRIDES
           ═══════════════════════════════════════════ */
        /* Dataframe / Table */
        div[data-testid="stDataFrame"] {
            border: 1px solid var(--border-default);
            border-radius: var(--radius-lg);
            overflow: hidden;
            background: var(--bg-surface);
        }
        div[data-testid="stDataFrame"] th {
            background: var(--bg-elevated) !important;
            color: var(--text-dim) !important;
            font-size: 11px !important;
            font-weight: 600 !important;
            letter-spacing: 0.03em !important;
            text-transform: uppercase !important;
            border-bottom: 1px solid var(--border-default) !important;
        }
        div[data-testid="stDataFrame"] td {
            background: var(--bg-surface) !important;
            color: var(--text-primary) !important;
            font-size: 13px !important;
            border-bottom: 1px solid var(--border-subtle) !important;
        }
        div[data-testid="stDataFrame"] tr:hover td {
            background: var(--bg-hover) !important;
        }

        /* Metric widget */
        div[data-testid="stMetric"] {
            border: 1px solid var(--border-default);
            background: var(--bg-surface);
            border-radius: var(--radius-lg);
            padding: var(--space-4);
        }
        div[data-testid="stMetric"] label {
            color: var(--text-muted) !important;
            font-size: 11px !important;
        }
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: var(--text-primary) !important;
            font-family: var(--font-mono) !important;
        }

        /* Progress bar */
        div.stProgress > div > div > div {
            background: linear-gradient(90deg, var(--accent-dim), var(--accent));
            border-radius: var(--radius-full);
        }
        div.stProgress > div > div {
            background: var(--bg-input);
            border-radius: var(--radius-full);
        }

        /* Select / Multiselect */
        div[data-baseweb="select"] > div {
            background: var(--bg-input) !important;
            border-color: var(--border-default) !important;
            border-radius: var(--radius-md) !important;
            color: var(--text-primary) !important;
        }

        /* Alert / Info / Warning */
        div[data-testid="stAlert"] {
            border-radius: var(--radius-lg);
            border: 1px solid var(--border-default);
            background: var(--bg-surface);
        }
        div[data-testid="stAlert"][kind="info"] {
            border-color: var(--border-accent);
            background: var(--accent-ghost);
        }

        /* Expander */
        .streamlit-expanderHeader {
            background: var(--bg-elevated) !important;
            border: 1px solid var(--border-default) !important;
            border-radius: var(--radius-md) !important;
            color: var(--text-secondary) !important;
            font-size: 13px !important;
        }
        .streamlit-expanderContent {
            background: transparent !important;
            border: none !important;
        }

        /* Tooltip */
        div[data-testid="stTooltip"] {
            background: var(--bg-elevated) !important;
            border: 1px solid var(--border-default) !important;
            border-radius: var(--radius-md) !important;
            color: var(--text-primary) !important;
        }

        /* ── Chart containers ─────────────────── */
        .chart-container {
            border: 1px solid var(--border-default);
            background: var(--bg-elevated);
            border-radius: var(--radius-lg);
            padding: var(--space-3);
        }

        /* ═══════════════════════════════════════════
           RESPONSIVE
           ═══════════════════════════════════════════ */
        @media (max-width: 900px) {
            .terminal-header { display: block; }
            .terminal-meta {
                min-width: 0;
                grid-template-columns: 1fr 1fr;
                margin-top: var(--space-4);
            }
            .terminal-title { font-size: 20px; }
            .metric-value { font-size: 22px; }
            .block-container { padding: var(--space-3) var(--space-3) var(--space-8); }
            .funnel-container { flex-wrap: wrap; }
            .funnel-arrow { display: none; }
        }

        @media (max-width: 640px) {
            .terminal-meta { grid-template-columns: 1fr; }
            .metric-card { min-height: auto; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def progress_widgets(st, title: str):
    box = st.container()
    with box:
        st.markdown(f'<div class="section-kicker">SYSTEM</div><div class="section-title">{title}</div>', unsafe_allow_html=True)
        bar = st.progress(0)
        status = st.empty()

    def update(message: str, current: int, total: int) -> None:
        ratio = 0 if total <= 0 else max(0.0, min(1.0, current / total))
        bar.progress(ratio)
        status.caption(f"{message} · {int(ratio * 100)}%")

    return update


def render_header(st, snapshot, latest_log) -> None:
    is_real_snapshot = snapshot is not None and snapshot["data_source"] == "tushare"
    source = snapshot["data_source"] if is_real_snapshot else "—"
    stock_count = snapshot["stock_count"] if is_real_snapshot else 0
    candidate_count = latest_log["candidate_count"] if latest_log is not None and is_real_snapshot else "—"

    regime_class = ""
    if latest_log is not None and is_real_snapshot:
        market = st.session_state.get("_last_market_score")
        if market is not None:
            score = float(market.get("total_score", 0))
            if score >= 75: regime_class = "active"
            elif score >= 65: regime_class = "tradable"
            elif score >= 50: regime_class = "watch"
            else: regime_class = "cautious"

    st.markdown(
        f"""
        <div class="terminal-header">
            <div>
                <div class="terminal-title">Mainline Pivot Scanner</div>
                <div class="terminal-subtitle">市场环境评分 · 行业主线确认 · 关键点突破 · 交易计划生成</div>
            </div>
            <div class="terminal-meta">
                <div class="meta-cell"><div class="meta-label">数据源</div><div class="meta-value">{source}</div></div>
                <div class="meta-cell"><div class="meta-label">覆盖股票</div><div class="meta-value">{stock_count}</div></div>
                <div class="meta-cell"><div class="meta-label">最新候选</div><div class="meta-value">{candidate_count}</div></div>
            </div>
        </div>
        <div class="risk-strip">⚠ {RISK_WARNING}</div>
        """,
        unsafe_allow_html=True,
    )


def render_regime_indicator(st, risk_level: str, total_score: float) -> None:
    level_map = {
        "积极": ("active", "积极交易 · 总仓位 60-80%"),
        "可交易": ("tradable", "可参与主线 · 总仓位 30-50%"),
        "观察": ("watch", "轻仓观察 · 总仓位 ≤ 30%"),
        "谨慎": ("cautious", "不建议开新仓 · 总仓位 ≤ 10%"),
    }
    cls, desc = level_map.get(risk_level, ("cautious", risk_level))
    st.markdown(
        f'<span class="regime-badge {cls}">{risk_level} · {total_score:.1f}分</span> '
        f'<span style="color:var(--text-muted);font-size:12px;">{desc}</span>',
        unsafe_allow_html=True,
    )


def render_snapshot_bar(st, snapshot) -> None:
    st.markdown(
        f"""
        <div class="snapshot-bar">
            <div class="status-pill">数据源 <strong>{snapshot['data_source']}</strong></div>
            <div class="status-pill">覆盖 <strong>{snapshot['stock_count']}</strong> 只</div>
            <div class="status-pill">最新交易日 <strong>{snapshot['latest_trade_date']}</strong></div>
            <div class="status-pill">加载 <strong>{snapshot['loaded_at']}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section(st, kicker: str, title: str) -> None:
    st.markdown(
        f'<div class="section-kicker">{kicker}</div><div class="section-title">{title}</div>',
        unsafe_allow_html=True,
    )


def metric_card(st, label: str, value, note: str = "", regime: str = "") -> None:
    regime_class = f"regime-{regime}" if regime else ""
    st.markdown(
        f"""
        <div class="metric-card {regime_class}">
            <div class="metric-label">{label}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_grid(st, items: list[tuple[str, object, str, str]]) -> None:
    cards = []
    for label, value, note, regime in items:
        regime_class = f"regime-{regime}" if regime else ""
        cards.append(
            "<div class=\"metric-card {regime_class}\">"
            "<div class=\"metric-label\">{label}</div>"
            "<div class=\"metric-value\">{value}</div>"
            "<div class=\"metric-note\">{note}</div>"
            "</div>".format(
                regime_class=regime_class,
                label=escape(str(label)),
                value=escape(str(value)),
                note=escape(str(note)),
            )
        )
    st.markdown(f"<div class=\"metric-grid\">{''.join(cards)}</div>", unsafe_allow_html=True)


def panel_title(st, title: str) -> None:
    st.markdown(f'<div class="panel-title">{title}</div>', unsafe_allow_html=True)


def rank_table(st, frame: pd.DataFrame, limit: int = 8) -> None:
    rows = []
    for _, row in frame.head(limit).iterrows():
        score_source = row["base_score"] if "base_score" in row and pd.notna(row["base_score"]) else row["score"]
        score = max(0.0, min(100.0, float(score_source)))
        confirmed = bool(row.get("confirmed", 0))
        candidate = bool(row.get("is_candidate_mainline", 0))
        drift = str(row.get("drift_status", "stable"))

        if confirmed:
            fill_class = "score-fill-hi"
            badge_cls, badge_txt = "badge-teal", "市场主线"
        elif candidate:
            fill_class = "score-fill-mid"
            badge_cls, badge_txt = "badge-blue", "候选主线"
        elif score >= 50:
            fill_class = "score-fill-mid"
            badge_cls, badge_txt = "badge-muted", "观察"
        else:
            fill_class = "score-fill-lo"
            badge_cls, badge_txt = "badge-muted", "普通"

        drift_label = ""
        if drift == "drift_warning":
            drift_label = '<span class="badge badge-amber" style="margin-left:4px;">漂移</span>'
        elif drift == "rank_up":
            drift_label = '<span class="badge badge-green" style="margin-left:4px;">上升</span>'

        if confirmed:
            stability_label = f"市场主线连续 {int(row.get('confirmed_stability_days', row.get('stability_days', 0)))} 日"
        elif candidate:
            stability_label = f"候选主线连续 {int(row.get('candidate_stability_days', 0))} 日"
        else:
            stability_label = "未连续确认"

        rows.append(
            "<tr>"
            f"<td><span class='rank-index'>#{int(row['rank'])}</span></td>"
            f"<td><div class='rank-name'>{escape(str(row['industry']))}</div>"
            f"<div class='rank-sub'>{stability_label}{drift_label}</div></td>"
            f"<td><span class='rank-value' style='color:var(--accent)'>{score:.1f}</span>"
            f"<div class='score-bar'><div class='score-fill {fill_class}' style='width:{score:.1f}%'></div></div></td>"
            f"<td><span class='badge {badge_cls}'>{badge_txt}</span></td>"
            "</tr>"
        )
    table_html = (
        "<table class='rank-table'>"
        "<thead><tr><th>#</th><th>行业</th><th>得分</th><th>状态</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    if hasattr(st, "html"):
        st.html(table_html)
    else:
        st.markdown(table_html, unsafe_allow_html=True)


def line_chart(st, pivot: pd.DataFrame) -> None:
    import altair as alt

    chart_data = pivot.reset_index().melt("trade_date", var_name="指数", value_name="收盘")
    chart_data["trade_date"] = pd.to_datetime(chart_data["trade_date"])
    chart = (
        alt.Chart(chart_data)
        .mark_line(strokeWidth=2.0, interpolate="monotone")
        .encode(
            x=alt.X("trade_date:T", title=None,
                    axis=alt.Axis(format="%m-%d", labelColor="#64748b", tickColor="#1e293b", grid=False)),
            y=alt.Y("收盘:Q", title=None, scale=alt.Scale(zero=False),
                    axis=alt.Axis(labelColor="#64748b", gridColor="#1e293b")),
            color=alt.Color(
                "指数:N", title=None,
                scale=alt.Scale(range=["#2dd4bf", "#60a5fa", "#f59e0b", "#a78bfa", "#fb7185"]),
                legend=alt.Legend(orient="bottom", labelColor="#94a3b8"),
            ),
            tooltip=[
                alt.Tooltip("trade_date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("指数:N", title="指数"),
                alt.Tooltip("收盘:Q", title="收盘", format=",.2f"),
            ],
        )
        .properties(height=340)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(chart, use_container_width=True)


def render_empty_state(st, title: str, body: str) -> None:
    st.markdown(
        f'<div class="empty-panel"><strong>{title}</strong>{body}</div>',
        unsafe_allow_html=True,
    )


def _latest_batch(conn) -> str:
    row = conn.execute("SELECT batch_id FROM run_log ORDER BY run_timestamp DESC LIMIT 1").fetchone()
    return row["batch_id"]


def current_snapshot(conn):
    try:
        row = conn.execute("SELECT * FROM data_snapshot WHERE id = 1").fetchone()
    except Exception:
        return None
    return row


def latest_run_log(conn):
    try:
        row = conn.execute("SELECT * FROM run_log ORDER BY run_timestamp DESC LIMIT 1").fetchone()
    except Exception:
        return None
    return row


def reusable_trade_dates(conn, keep_recent: int = 5) -> set[str]:
    rows = conn.execute("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date").fetchall()
    dates = [str(row["trade_date"]) for row in rows]
    if len(dates) <= keep_recent:
        return set()
    return set(dates[:-keep_recent])


def render_market_overview(st, conn) -> None:
    batch_id = _latest_batch(conn)
    market = read_sql(conn, "SELECT * FROM market_score WHERE batch_id = ?", (batch_id,))
    latest = market.iloc[0]
    total_score = float(latest["total_score"])
    risk_level = str(latest["risk_level"])

    render_section(st, "Market Regime", "市场总览")

    # Regime indicator
    render_regime_indicator(st, risk_level, total_score)

    # Responsive metric grid. Avoid Streamlit columns getting too narrow and forcing vertical text.
    scored_cols = [
        ("指数趋势", latest["index_trend_score"], "均线多头结构", "active" if float(latest["index_trend_score"]) >= 20 else "watch"),
        ("赚钱效应", latest["profit_effect_score"], "上涨比例与强势股", "active" if float(latest["profit_effect_score"]) >= 20 else "watch"),
        ("成交活跃", latest["activity_score"], "成交额相对均量", "active" if float(latest["activity_score"]) >= 10 else "watch"),
        ("情绪温度", latest["sentiment_score"], "涨跌停与极端值", "active" if float(latest["sentiment_score"]) >= 10 else "watch"),
        ("风格一致", latest["style_consistency_score"], "核心指数MA20一致性", ""),
        ("总评", round(total_score, 1), risk_level, risk_level_map_class(risk_level)),
    ]
    metric_grid(st, scored_cols)

    # Charts row
    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
    index_daily = read_sql(
        conn,
        """
        SELECT index_name, trade_date, close
        FROM index_daily
        WHERE trade_date >= (SELECT date(MAX(trade_date), '-90 day') FROM index_daily)
        ORDER BY trade_date
        """,
    )
    pivot = index_daily.pivot(index="trade_date", columns="index_name", values="close")
    left, right = st.columns([2, 1])
    with left:
        with st.container(border=True):
            panel_title(st, "核心指数近90日走势")
            line_chart(st, pivot)
    with right:
        industry = read_sql(
            conn,
            """
            SELECT industry, rank, score, base_score, confirmed, is_candidate_mainline,
                   candidate_stability_days, confirmed_stability_days, drift_status, stability_days
            FROM industry_score
            WHERE batch_id = ?
            ORDER BY rank
            LIMIT 8
            """,
            (batch_id,),
        )
        with st.container(border=True):
            panel_title(st, "行业主线 Top 8")
            rank_table(st, industry)

    # Market commentary
    _render_market_commentary(st, latest, industry)

    # Pre-watch candidates (stocks passing individual filters but industry not confirmed)
    watch_candidates = read_sql(
        conn,
        """
        SELECT code, name, industry, trend_template_type, volume_quality, close_quality, keypoint_type
        FROM strategy_result
        WHERE batch_id = ?
          AND status = 'excluded'
          AND exclude_reason = '所属行业未确认市场主线'
        ORDER BY industry, code
        """,
        (batch_id,),
    )
    if not watch_candidates.empty:
        with st.container(border=True):
            panel_title(st, f"预备观察 · {len(watch_candidates)} 只个股通过技术筛选但行业未确认主线")
            st.dataframe(
                watch_candidates.rename(
                    columns={
                        "code": "代码",
                        "name": "名称",
                        "industry": "行业",
                        "trend_template_type": "趋势类型",
                        "volume_quality": "量能质量",
                        "close_quality": "收盘质量",
                        "keypoint_type": "关键点类型",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )


def risk_level_map_class(level: str) -> str:
    return {"积极": "active", "可交易": "tradable", "观察": "watch", "谨慎": "cautious"}.get(level, "")


def _render_market_commentary(st, latest, industry: pd.DataFrame) -> None:
    total = float(latest["total_score"])
    trend = float(latest["index_trend_score"])
    profit = float(latest["profit_effect_score"])
    sentiment = float(latest["sentiment_score"])
    activity = float(latest["activity_score"])
    style = float(latest["style_consistency_score"])

    if total >= 75:
        summary = "市场环境积极，赚钱效应显著，适合积极参与已确认主线中的核心趋势股。"
    elif total >= 65:
        summary = "市场环境可交易，赚钱效应尚可，建议聚焦主线核心标的，控制追高节奏。"
    elif total >= 50:
        summary = "市场环境偏弱，赚钱效应一般。仅建议轻仓观察已确认主线内的A类趋势股，不急于开仓。"
    else:
        summary = "市场环境谨慎，上涨比例偏低。建议以观察为主，不主动开新仓，关注主线演变方向。"

    details = []
    if trend < 15: details.append("指数均线结构偏弱")
    if profit < 15: details.append("个股赚钱效应不足")
    if sentiment < 8: details.append("市场情绪偏低")
    if activity < 8: details.append("成交活跃度不足")
    if style < 5: details.append("行业风格分化明显")
    detail_str = "；".join(details) if details else "各项指标正常"

    st.info(f"{summary} ({detail_str})")


def render_mainline_radar(st, conn) -> None:
    batch_id = _latest_batch(conn)
    industry = read_sql(
        conn,
        """
        SELECT trade_date, industry, rank, score, base_score, momentum_score, breadth_score, amount_score,
               total_score, persistence_score, strength_score, width_score, capacity_score,
               hot_score, leader_score, leader_250_high_count, leader_60_high_count,
               leader_trend_middle_count, logic_score,
               mainline_status, strong_streak_days, rank_top5_avg, amount_ratio,
               is_watch_mainline, is_near_confirm, is_downtrend_watch,
               confirmed, is_candidate_mainline, candidate_stability_days, confirmed_stability_days,
               stability_days, rank_change, score_change,
               drift_flag, drift_status
        FROM industry_score
        WHERE batch_id = ?
        ORDER BY rank
        """,
        (batch_id,),
    )
    industry = _attach_industry_detail_metrics(conn, batch_id, industry)
    industry = _attach_mainline_stock_pool_counts(conn, batch_id, industry)
    if "mainline_status" not in industry.columns:
        industry["mainline_status"] = industry.apply(_mainline_status_label, axis=1)
    history_days = _industry_score_history_days(conn)
    cold_start = history_days < 3
    industry["mainline_note"] = industry.apply(
        lambda row: "得分较高，但稳定天数不足，暂未确认主线"
        if float(row.get("base_score", 0) or 0) >= 70 and int(row.get("confirmed", 0) or 0) != 1
        else "",
        axis=1,
    )

    render_section(st, "Mainline Radar", "主线雷达 + 行业明细")
    if cold_start:
        st.info(f"当前处于主线冷启动阶段：已有 {history_days} 个交易日评分历史，主线确认至少需要 3-5 个交易日评分历史。正式交易规则不会因冷启动而放宽。")

    # Kicker stats
    confirmed_count = int((industry["mainline_status"] == "确认主线").sum())
    near_count = int((industry["mainline_status"] == "接近确认").sum())
    candidate_count = int((industry["mainline_status"] == "候选主线").sum())
    watch_count = int((industry["mainline_status"] == "主线预警").sum())
    downtrend_count = int((industry["mainline_status"] == "退潮观察").sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1: metric_card(st, "确认主线", f"{confirmed_count} 个", "连续3日得分≥80", "active" if confirmed_count > 0 else "")
    with c2: metric_card(st, "接近确认", f"{near_count} 个", "75≤得分<80且持续", "tradable" if near_count > 0 else "")
    with c3: metric_card(st, "候选主线", f"{candidate_count} 个", "3日内至少2日≥70", "tradable" if candidate_count > 0 else "")
    with c4: metric_card(st, "主线预警", f"{watch_count} 个", "强度+资金异动", "cautious" if watch_count > 0 else "")
    with c5: metric_card(st, "退潮观察", f"{downtrend_count} 个", "连续跌回65下方", "cautious" if downtrend_count > 0 else "")

    # Mainline status legend
    st.markdown(
        '<div style="margin:12px 0 8px;display:flex;gap:16px;font-size:11px;color:var(--text-muted)">'
        '<span class="badge badge-teal">确认主线</span>'
        '<span class="badge badge-blue">接近确认</span>'
        '<span class="badge badge-blue">候选主线</span>'
        '<span class="badge badge-amber">主线预警</span>'
        '<span class="badge badge-muted">退潮观察/普通</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    filter_opts = ["全部", "仅确认主线", "仅接近确认", "仅候选主线", "仅主线预警", "仅退潮观察"]
    selected_filter = st.selectbox("筛选", filter_opts, label_visibility="collapsed")
    filtered_industry = industry
    if selected_filter == "仅确认主线":
        filtered_industry = industry[industry["mainline_status"] == "确认主线"]
    elif selected_filter == "仅接近确认":
        filtered_industry = industry[industry["mainline_status"] == "接近确认"]
    elif selected_filter == "仅候选主线":
        filtered_industry = industry[industry["mainline_status"] == "候选主线"]
    elif selected_filter == "仅主线预警":
        filtered_industry = industry[industry["mainline_status"] == "主线预警"]
    elif selected_filter == "仅退潮观察":
        filtered_industry = industry[industry["mainline_status"] == "退潮观察"]

    detail_cols = [
        "industry", "base_score", "total_score", "mainline_status", "strong_streak_days",
        "candidate_stability_days", "confirmed_stability_days", "rank_top5_avg", "amount_multiple",
        "strength_score", "capacity_score", "leader_score", "leader_250_high_count",
        "leader_60_high_count", "leader_trend_middle_count", "is_watch_mainline",
        "is_candidate_mainline", "is_near_confirm", "confirmed", "is_downtrend_watch",
        "warning_stock_count", "focus_stock_count", "formal_stock_count",
        "rank", "ret5_pct", "ret10_pct", "ret20_pct", "ma20_above_ratio", "mainline_note"
    ]
    detail_display = filtered_industry[detail_cols].rename(columns={
        "industry": "行业名称",
        "base_score": "主线基础得分",
        "total_score": "总分",
        "mainline_status": "主线状态",
        "strong_streak_days": "连续强度天数",
        "candidate_stability_days": "候选确认天数",
        "confirmed_stability_days": "确认主线天数",
        "rank_top5_avg": "近5日强度排名",
        "amount_multiple": "成交额放大倍数",
        "strength_score": "强度",
        "capacity_score": "资金容量",
        "leader_score": "龙头结构",
        "leader_250_high_count": "250日新高个股数",
        "leader_60_high_count": "60日新高个股数",
        "leader_trend_middle_count": "趋势中军个股数",
        "is_watch_mainline": "是否主线预警",
        "is_candidate_mainline": "是否候选主线",
        "is_near_confirm": "是否接近确认",
        "confirmed": "是否确认主线",
        "is_downtrend_watch": "是否退潮观察",
        "warning_stock_count": "预警个股数",
        "focus_stock_count": "重点观察个股数",
        "formal_stock_count": "正式候选个股数",
        "rank": "排名",
        "ret5_pct": "5日涨幅%",
        "ret10_pct": "10日涨幅%",
        "ret20_pct": "20日涨幅%",
        "ma20_above_ratio": "MA20上方个股占比%",
        "mainline_note": "确认说明",
        "rank_change": "排名变化",
        "score_change": "得分变化",
        "drift_status": "漂移状态",
    })
    st.dataframe(detail_display, use_container_width=True, hide_index=True)

    # Score breakdown chart
    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)
    chart_data = filtered_industry.set_index("industry")[
        ["persistence_score", "strength_score", "width_score", "capacity_score", "leader_score"]
    ].head(10)
    st.bar_chart(chart_data, height=300)


def _attach_mainline_stock_pool_counts(conn, batch_id: str, industry: pd.DataFrame) -> pd.DataFrame:
    if industry.empty:
        return industry
    counts = read_sql(
        conn,
        """
        SELECT industry, candidate_layer, COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
          AND candidate_layer IN ('预警个股池', '重点观察个股池', '正式候选股池')
        GROUP BY industry, candidate_layer
        """,
        (batch_id,),
    )
    enriched = industry.copy()
    for column in ["warning_stock_count", "focus_stock_count", "formal_stock_count"]:
        enriched[column] = 0
    if counts.empty:
        return enriched
    pivot = counts.pivot_table(index="industry", columns="candidate_layer", values="cnt", aggfunc="sum", fill_value=0)
    rename = {
        "预警个股池": "warning_stock_count",
        "重点观察个股池": "focus_stock_count",
        "正式候选股池": "formal_stock_count",
    }
    pivot = pivot.rename(columns=rename)
    keep = [col for col in rename.values() if col in pivot.columns]
    enriched = enriched.drop(columns=[col for col in keep if col in enriched.columns], errors="ignore").merge(
        pivot[keep].reset_index(),
        on="industry",
        how="left",
    )
    for column in ["warning_stock_count", "focus_stock_count", "formal_stock_count"]:
        enriched[column] = enriched[column].fillna(0).astype(int)
    return enriched


def _mainline_status_label(row: pd.Series) -> str:
    if int(row.get("drift_flag", 0)) == 1:
        return "退潮观察"
    if int(row.get("confirmed", 0)) == 1:
        return "确认主线"
    if int(row.get("is_near_confirm", 0)) == 1:
        return "接近确认"
    if int(row.get("is_candidate_mainline", 0)) == 1:
        return "候选主线"
    if int(row.get("is_watch_mainline", 0)) == 1:
        return "主线预警"
    return "普通"


def _industry_score_history_days(conn) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT trade_date) AS days FROM industry_score").fetchone()
    return int(row["days"] or 0) if row else 0


def _attach_industry_detail_metrics(conn, batch_id: str, industry: pd.DataFrame) -> pd.DataFrame:
    if industry.empty:
        return industry
    enriched = industry.copy()
    if "trade_date" not in enriched.columns:
        row = conn.execute("SELECT trade_date FROM run_log WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row:
            return enriched
        enriched["trade_date"] = str(row["trade_date"])
    trade_date = str(enriched["trade_date"].iloc[0])

    daily = read_sql(
        conn,
        """
        SELECT industry, trade_date, close, amount
        FROM industry_daily
        WHERE trade_date <= ?
        ORDER BY industry, trade_date
        """,
        (trade_date,),
    )
    detail_rows = []
    for name, group in daily.groupby("industry"):
        group = group.sort_values("trade_date").tail(30)
        latest = group.iloc[-1] if not group.empty else None
        if latest is None:
            continue
        row = {"industry": name}
        for days, col in ((5, "ret5_pct"), (10, "ret10_pct"), (20, "ret20_pct")):
            row[col] = None
            if len(group) > days:
                base = float(group.iloc[-days - 1]["close"])
                if base > 0:
                    row[col] = round((float(latest["close"]) / base - 1) * 100, 2)
        amount_ma20 = float(group["amount"].tail(20).mean()) if not group.empty else 0.0
        row["amount_multiple"] = round(float(latest["amount"]) / amount_ma20, 2) if amount_ma20 > 0 else None
        detail_rows.append(row)

    if detail_rows:
        enriched = enriched.merge(pd.DataFrame(detail_rows), on="industry", how="left")

    dates = read_sql(
        conn,
        """
        SELECT DISTINCT trade_date
        FROM stock_daily
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 30
        """,
        (trade_date,),
    )
    if not dates.empty:
        placeholders = ",".join(["?"] * len(dates))
        stock_daily = read_sql(
            conn,
            f"""
            SELECT code, trade_date, close
            FROM stock_daily
            WHERE trade_date IN ({placeholders})
            """,
            tuple(dates["trade_date"].tolist()),
        )
        stock_basic = read_sql(conn, "SELECT code, industry FROM stock_basic")
        if not stock_daily.empty and not stock_basic.empty:
            stock_daily = stock_daily.sort_values(["code", "trade_date"])
            stock_daily["ma20"] = stock_daily.groupby("code")["close"].transform(
                lambda s: s.rolling(20, min_periods=20).mean()
            )
            latest_stock = stock_daily[stock_daily["trade_date"] == trade_date].copy()
            latest_stock = latest_stock.merge(stock_basic, on="code", how="left")
            latest_stock["above_ma20"] = latest_stock["close"] > latest_stock["ma20"]
            ratio = latest_stock.groupby("industry")["above_ma20"].mean().mul(100).round(2).reset_index()
            ratio = ratio.rename(columns={"above_ma20": "ma20_above_ratio"})
            enriched = enriched.merge(ratio, on="industry", how="left")

    for col in ("ret5_pct", "ret10_pct", "ret20_pct", "amount_multiple", "ma20_above_ratio"):
        if col not in enriched.columns:
            enriched[col] = None
    return enriched


def render_candidates(st, conn) -> None:
    batch_id = _latest_batch(conn)
    render_section(st, "Candidate Pool", "个股候选池")

    # ── Funnel Visualization ──
    _render_funnel(st, conn, batch_id)

    # ── Included Candidates ──
    results = read_sql(
        conn,
        """
        SELECT code, name, industry, status, candidate_layer, mainline_status, mainline_base_score,
               keypoint_distance_pct, signal_status, market_score, industry_score,
               fundamental_status, trend_template_type, volume_quality, close_quality,
               risk_level, keypoint_date, keypoint_price, keypoint_type,
               breakout_date, breakout_close, breakout_day_low, breakout_ma10,
               pullback_low, confirm_date, confirm_close, pullback_volume_shrink,
               confirm_volume_expand, trade_plan_type, suggested_action,
               watch_price, trigger_price, buy_lower, buy_upper, suggested_buy_price,
               stop_loss_price, take_profit_1, take_profit_2, trailing_stop_rule,
               suggested_position, include_reason, exclude_reason, risk_warning
        FROM strategy_result
        WHERE batch_id = ? AND status = 'included'
        ORDER BY industry_score DESC, keypoint_price DESC
        """,
        (batch_id,),
    )

    if results.empty:
        st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
        render_empty_state(st, "本批次没有最终候选股",
                           "当前没有股票同时通过基本面硬过滤、趋势模板、关键点突破、突破质量和行业主线确认。<br>"
                           "下方显示预备观察和剔除分析。")
    else:
        # Summary bar
        c1, c2, c3 = st.columns(3)
        with c1:
            leader_count = int((results["trade_plan_type"] == "龙头突破试错计划").sum())
            metric_card(st, "龙头突破试错", f"{leader_count} 只", "A类趋势 + 行业前10% + 温和放量高点", "active")
        with c2:
            pullback_count = int((results["trade_plan_type"] == "中军回踩确认计划").sum())
            metric_card(st, "中军回踩确认", f"{pullback_count} 只", "回踩不破位 + 再次放量收阳", "tradable")
        with c3:
            watch_only = int((results["suggested_action"] == "仅观察").sum())
            metric_card(st, "仅观察", f"{watch_only} 只", "市场评分＜65 不输出买入区间", "watch" if watch_only > 0 else "")

        # Candidate table
        display_cols = [
            "code", "name", "industry", "trend_template_type",
            "keypoint_type", "keypoint_price", "trade_plan_type", "suggested_action",
            "buy_lower", "buy_upper", "suggested_buy_price",
            "stop_loss_price", "take_profit_1", "take_profit_2",
            "suggested_position"
        ]
        st.dataframe(results[display_cols], use_container_width=True, hide_index=True,
                     column_config={
                         "buy_lower": "买入下限",
                         "buy_upper": "买入上限",
                         "suggested_buy_price": "建议买入价",
                         "stop_loss_price": "止损价",
                         "take_profit_1": "止盈1",
                         "take_profit_2": "止盈2",
                         "suggested_position": "建议仓位%",
                         "trend_template_type": "趋势",
                         "keypoint_type": "突破类型",
                         "keypoint_price": "突破价",
                         "trade_plan_type": "计划类型",
                         "suggested_action": "建议动作",
                     })

        # Detail table
        if len(results) <= 20:
            st.markdown('<div class="panel-title">完整交易计划明细</div>', unsafe_allow_html=True)
            with st.container(border=True):
                detail_cols = [
                    "code", "name", "signal_status", "trend_template_type",
                    "keypoint_date", "keypoint_price", "keypoint_type",
                    "volume_quality", "close_quality", "trade_plan_type",
                    "watch_price", "trigger_price", "buy_lower", "buy_upper",
                    "suggested_buy_price", "stop_loss_price",
                    "take_profit_1", "take_profit_2", "trailing_stop_rule",
                    "suggested_position", "risk_level", "include_reason", "risk_warning"
                ]
                st.dataframe(results[detail_cols], use_container_width=True, hide_index=True)

        # Export
        export_cols = [col for col in CANDIDATE_EXPORT_COLUMNS if col in results.columns]
        csv = results[export_cols].to_csv(index=False).encode("utf-8")
        st.download_button("⭳ 导出候选股 CSV", csv,
                          f"candidates_{batch_id}.csv", "text/csv",
                          use_container_width=True)

    _render_layered_candidates(st, conn, batch_id)

    # ── Pre-watch & Excluded ──
    _render_excluded_analysis(st, conn, batch_id)


def _render_layered_candidates(st, conn, batch_id: str) -> None:
    layered = read_sql(
        conn,
        """
        SELECT code, name, industry, candidate_layer, mainline_status, mainline_base_score,
               signal_status, market_score, industry_score, fundamental_status, trend_template_type,
               keypoint_type, keypoint_price, keypoint_distance_pct,
               volume_quality, close_quality, suggested_action, watch_price, trigger_price,
               buy_lower, buy_upper, stop_loss_price, take_profit_1, take_profit_2,
               suggested_position, risk_warning, rejected_reason_detail, exclude_reason
        FROM strategy_result
        WHERE batch_id = ?
          AND candidate_layer IN ('预警个股池', '重点观察个股池', '正式候选股池', '技术突破候选', '接近候选')
        ORDER BY CASE candidate_layer
                    WHEN '正式候选股池' THEN 1
                    WHEN '重点观察个股池' THEN 2
                    WHEN '预警个股池' THEN 3
                    WHEN '技术突破候选' THEN 4
                    WHEN '接近候选' THEN 5
                    ELSE 9
                 END,
                 industry_score DESC,
                 keypoint_price DESC
        LIMIT 300
        """,
        (batch_id,),
    )
    if layered.empty:
        return

    with st.container(border=True):
        panel_title(st, "分层候选池")
        st.caption("正式候选规则未放宽；预警、重点观察、技术突破和接近候选仅用于诊断，不生成正式买入区间。")
        layer_counts = layered["candidate_layer"].value_counts().to_dict()
        metric_grid(
            st,
            [
                ("预警个股池", layer_counts.get("预警个股池", 0), "65分主线预警，提前发现", "watch"),
                ("重点观察个股池", layer_counts.get("重点观察个股池", 0), "75分接近确认或确认内等待回踩", "tradable"),
                ("正式候选股池", layer_counts.get("正式候选股池", 0), "满足全部正式交易条件", "active"),
                ("技术突破候选", layer_counts.get("技术突破候选", 0), "趋势和关键点合格", "tradable"),
                ("接近候选", layer_counts.get("接近候选", 0), "仅差1个条件", ""),
            ],
        )
        filter_cols = st.columns(4)
        layer_options = ["全部"] + sorted(layered["candidate_layer"].dropna().astype(str).unique().tolist())
        status_options = ["全部"] + sorted(layered["mainline_status"].dropna().astype(str).unique().tolist())
        trend_options = ["全部"] + sorted(layered["trend_template_type"].dropna().astype(str).unique().tolist())
        action_options = ["全部"] + sorted(layered["suggested_action"].fillna("").astype(str).unique().tolist())
        selected_layer = filter_cols[0].selectbox("候选层级", layer_options)
        selected_status = filter_cols[1].selectbox("主线状态", status_options)
        selected_trend = filter_cols[2].selectbox("趋势模板", trend_options)
        selected_action = filter_cols[3].selectbox("建议动作", action_options)
        filtered = layered.copy()
        if selected_layer != "全部":
            filtered = filtered[filtered["candidate_layer"] == selected_layer]
        if selected_status != "全部":
            filtered = filtered[filtered["mainline_status"] == selected_status]
        if selected_trend != "全部":
            filtered = filtered[filtered["trend_template_type"] == selected_trend]
        if selected_action != "全部":
            filtered = filtered[filtered["suggested_action"].fillna("") == selected_action]
        display = filtered.rename(
            columns={
                "code": "代码",
                "name": "名称",
                "industry": "行业",
                "candidate_layer": "候选层级",
                "mainline_status": "主线状态",
                "mainline_base_score": "主线基础分",
                "signal_status": "信号状态",
                "market_score": "市场分",
                "industry_score": "行业分",
                "fundamental_status": "基本面",
                "trend_template_type": "趋势",
                "keypoint_type": "关键点",
                "keypoint_price": "关键价",
                "keypoint_distance_pct": "关键点距离",
                "volume_quality": "量能质量",
                "close_quality": "收盘质量",
                "suggested_action": "建议动作",
                "watch_price": "观察价",
                "trigger_price": "触发价",
                "buy_lower": "买入下限",
                "buy_upper": "买入上限",
                "stop_loss_price": "止损价",
                "take_profit_1": "止盈1",
                "take_profit_2": "止盈2",
                "suggested_position": "建议仓位",
                "risk_warning": "风险提示",
                "rejected_reason_detail": "诊断原因",
                "exclude_reason": "过滤说明",
            }
        )
        st.dataframe(display, use_container_width=True, hide_index=True)


def _render_funnel(st, conn, batch_id: str, expanded: bool = True, show_summary: bool = True) -> None:
    """Render a visual funnel showing stocks passing through each filter stage."""
    log = conn.execute(
        """SELECT total_stock_count, after_basic_filter_count, after_fundamental_filter_count,
                  after_trend_filter_count, after_keypoint_filter_count,
                  after_mainline_filter_count, after_market_filter_count, candidate_count
           FROM run_log WHERE batch_id = ?""",
        (batch_id,),
    ).fetchone()

    if not log:
        return

    total = int(log["total_stock_count"]) if log["total_stock_count"] else 0
    if total == 0:
        return  # old batch without funnel data
    after_basic = int(log["after_basic_filter_count"]) if log["after_basic_filter_count"] else 0
    after_fund = int(log["after_fundamental_filter_count"]) if log["after_fundamental_filter_count"] else 0
    after_trend = int(log["after_trend_filter_count"]) if log["after_trend_filter_count"] else 0
    after_kp = int(log["after_keypoint_filter_count"]) if log["after_keypoint_filter_count"] else 0
    after_mainline = int(log["after_mainline_filter_count"]) if "after_mainline_filter_count" in log.keys() and log["after_mainline_filter_count"] else 0
    after_market = int(log["after_market_filter_count"]) if "after_market_filter_count" in log.keys() and log["after_market_filter_count"] else 0
    included = int(log["candidate_count"]) if log["candidate_count"] else 0
    layer_counts = _candidate_layer_counts(conn, batch_id)

    stages = [
        ("全市场", total, ""),
        ("基础过滤", after_basic, "ST/停牌/低价/流动性"),
        ("基本面", after_fund, "净利润/扣非/营收/负债"),
        ("趋势模板", after_trend, "A/B类趋势股"),
        ("关键点突破", after_kp, "温和放量新高原台"),
        ("主线确认", after_mainline, "行业3日确认主线"),
        ("市场评分", after_market, "总分≥65才可正式买入"),
        ("最终候选", included, ""),
    ]

    st.markdown('<div class="panel-title">筛选漏斗 · 逐层过滤统计</div>', unsafe_allow_html=True)
    with st.container(border=True):
        html = '<div class="funnel-container">'
        for i, (label, count, desc) in enumerate(stages):
            if i > 0:
                prev = stages[i - 1][1]
                dropped = prev - count if prev > count else 0
                html += (
                    f'<div class="funnel-arrow">'
                    f'<div style="font-size:9px;color:var(--danger);margin-bottom:2px;">-{dropped}</div>'
                    f'<span style="color:var(--text-dim)">→</span></div>'
                )
            html += (
                f'<div class="funnel-step">'
                f'<div class="funnel-count">{count}</div>'
                f'<div class="funnel-label">{label}</div>'
                f'</div>'
            )
        html += '</div>'
        st.markdown(html, unsafe_allow_html=True)
        funnel_fields = pd.DataFrame(
            [
                {"环节": FUNNEL_LABELS["total_stock_count"][0], "数量": total, "说明": FUNNEL_LABELS["total_stock_count"][1]},
                {
                    "环节": FUNNEL_LABELS["after_basic_filter_count"][0],
                    "数量": after_basic,
                    "说明": FUNNEL_LABELS["after_basic_filter_count"][1],
                },
                {
                    "环节": FUNNEL_LABELS["after_fundamental_filter_count"][0],
                    "数量": after_fund,
                    "说明": FUNNEL_LABELS["after_fundamental_filter_count"][1],
                },
                {
                    "环节": FUNNEL_LABELS["after_trend_filter_count"][0],
                    "数量": after_trend,
                    "说明": FUNNEL_LABELS["after_trend_filter_count"][1],
                },
                {
                    "环节": FUNNEL_LABELS["after_keypoint_filter_count"][0],
                    "数量": after_kp,
                    "说明": FUNNEL_LABELS["after_keypoint_filter_count"][1],
                },
                {"环节": "预警个股池", "数量": int(layer_counts.get("预警个股池", 0)), "说明": "65分主线预警方向，提前观察"},
                {"环节": "重点观察个股池", "数量": int(layer_counts.get("重点观察个股池", 0)), "说明": "75分接近确认或确认主线内等待回踩"},
                {"环节": "正式候选股池", "数量": int(layer_counts.get("正式候选股池", 0)), "说明": "满足正式交易计划条件"},
                {
                    "环节": FUNNEL_LABELS["after_mainline_filter_count"][0],
                    "数量": after_mainline,
                    "说明": FUNNEL_LABELS["after_mainline_filter_count"][1],
                },
                {
                    "环节": FUNNEL_LABELS["after_market_filter_count"][0],
                    "数量": after_market,
                    "说明": FUNNEL_LABELS["after_market_filter_count"][1],
                },
                {"环节": FUNNEL_LABELS["candidate_count"][0], "数量": included, "说明": FUNNEL_LABELS["candidate_count"][1]},
            ]
        )
        st.dataframe(funnel_fields, use_container_width=True, hide_index=True)
        if show_summary and included == 0:
            reasons = _candidate_empty_reasons(conn, batch_id, total, after_trend, after_kp, after_mainline)
            if reasons:
                st.warning("；".join(reasons))
            _render_rejected_top_reasons(st, conn, batch_id)


def _candidate_empty_reasons(
    conn,
    batch_id: str,
    total: int,
    after_trend: int,
    after_keypoint: int,
    after_mainline: int,
) -> list[str]:
    reasons: list[str] = []
    market = conn.execute("SELECT total_score FROM market_score WHERE batch_id = ?", (batch_id,)).fetchone()
    market_score = float(market["total_score"]) if market and market["total_score"] is not None else 0.0
    layer_counts = _candidate_layer_counts(conn, batch_id)
    warning_pool = int(layer_counts.get("预警个股池", 0))
    focus_pool = int(layer_counts.get("重点观察个股池", 0))
    if warning_pool > 0 or focus_pool > 0:
        reasons.append(
            f"正式候选为0，但预警个股池 {warning_pool} 只、重点观察个股池 {focus_pool} 只，说明主线正在形成或个股接近条件"
        )
    if market_score < 65:
        reasons.append("市场评分低于65，不生成正式买入建议")
    if _industry_score_history_days(conn) < 3:
        reasons.append("当前处于主线冷启动阶段，主线确认至少需要3-5个交易日评分历史")
    if after_mainline == 0 and after_keypoint > 0:
        reasons.append("主线未确认，技术突破个股暂不能进入正式候选")
    status_counts = read_sql(
        conn,
        """
        SELECT mainline_status, COUNT(*) AS cnt
        FROM industry_score
        WHERE batch_id = ?
        GROUP BY mainline_status
        """,
        (batch_id,),
    )
    status_map = dict(zip(status_counts["mainline_status"], status_counts["cnt"])) if not status_counts.empty else {}
    if int(status_map.get("主线预警", 0)) > 0:
        reasons.append("当前存在主线预警方向，但尚未达到正式候选或确认主线标准。建议观察主线连续性和核心个股回踩机会")
    if int(status_map.get("接近确认", 0)) > 0:
        reasons.append("当前存在接近确认主线，市场方向正在形成。可重点观察A类趋势股、龙头突破和中军回踩机会")
    if after_keypoint == 0:
        reasons.append("通过关键点突破的个股数量为0")
    if after_keypoint > after_mainline:
        reasons.append("存在个股通过技术筛选但行业未确认市场主线")
    if total > 0 and after_trend / total < 0.05:
        reasons.append("趋势模板过滤后剩余数量过少")
    fund_missing = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
          AND rejected_reason_detail = '财务数据缺失或代理字段不足'
        """,
        (batch_id,),
    ).fetchone()
    if fund_missing and int(fund_missing["cnt"] or 0) > 0:
        reasons.append("基本面数据缺失或代理字段不足，导致部分股票无法进入候选")
    if not reasons:
        reasons.append("当前没有股票同时满足基本面、趋势、关键点、主线和市场条件")
    return reasons


def _candidate_layer_counts(conn, batch_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT candidate_layer, COUNT(*) AS cnt
        FROM strategy_result
        WHERE batch_id = ?
        GROUP BY candidate_layer
        """,
        (batch_id,),
    ).fetchall()
    counts = {str(row["candidate_layer"]): int(row["cnt"]) for row in rows}
    for layer in ("预警个股池", "重点观察个股池", "正式候选股池", "技术突破候选", "接近候选", "剔除"):
        counts.setdefault(layer, 0)
    return counts


def _render_rejected_top_reasons(st, conn, batch_id: str) -> None:
    top_reasons = read_sql(
        conn,
        """
        SELECT rejected_reason_detail AS 主要原因, COUNT(*) AS 数量
        FROM strategy_result
        WHERE batch_id = ?
          AND COALESCE(rejected_reason_detail, '') <> ''
        GROUP BY rejected_reason_detail
        ORDER BY 数量 DESC
        LIMIT 5
        """,
        (batch_id,),
    )
    if top_reasons.empty:
        return
    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
    panel_title(st, "候选为 0 的主要原因 Top 5")
    st.dataframe(top_reasons, use_container_width=True, hide_index=True)


def _render_excluded_analysis(st, conn, batch_id: str) -> None:
    """Render pre-watch and excluded reason breakdown."""
    # Pre-watch: passed individual but industry not confirmed
    watch_candidates = read_sql(
        conn,
        """
        SELECT code, name, industry, trend_template_type, volume_quality, close_quality, keypoint_type
        FROM strategy_result
        WHERE batch_id = ? AND status = 'excluded'
          AND exclude_reason = '所属行业未确认市场主线'
        ORDER BY industry, code
        """,
        (batch_id,),
    )
    if not watch_candidates.empty:
        with st.container(border=True):
            panel_title(st, f"预备观察 · {len(watch_candidates)} 只个股通过技术筛选但行业未确认主线")
            st.caption("以下个股已通过基本面、趋势模板、关键点突破和突破质量过滤，仅因所属行业未连续3日确认主线而暂未入选。")
            st.dataframe(
                watch_candidates.rename(
                    columns={
                        "code": "代码",
                        "name": "名称",
                        "industry": "行业",
                        "trend_template_type": "趋势类型",
                        "volume_quality": "量能质量",
                        "close_quality": "收盘质量",
                        "keypoint_type": "关键点类型",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

    # Exclude reason summary
    reason_summary = read_sql(
        conn,
        """
        SELECT exclude_reason, COUNT(*) AS count
        FROM strategy_result
        WHERE batch_id = ? AND status = 'excluded'
        GROUP BY exclude_reason
        ORDER BY count DESC
        LIMIT 25
        """,
        (batch_id,),
    )
    with st.container(border=True):
        panel_title(st, "剔除原因分布 · Top 25")
        st.dataframe(
            reason_summary.rename(columns={"exclude_reason": "剔除原因", "count": "数量"}),
            use_container_width=True,
            hide_index=True,
        )


def render_watch_pool(st, conn) -> None:
    watch = read_sql(
        conn,
        """
        SELECT code, name, industry, source_batch_id, added_at,
               trade_plan_type, suggested_action, watch_price, trigger_price,
               buy_lower, buy_upper, suggested_buy_price, stop_loss_price,
               take_profit_1, take_profit_2, trailing_stop_rule,
               suggested_position, risk_warning, note
        FROM watch_pool ORDER BY added_at DESC
        """
    )
    render_section(st, "Watchlist", "观察池")
    if watch.empty:
        render_empty_state(st, "观察池为空", "策略产生候选后会自动进入观察池。")
        return

    # Stats
    c1, c2 = st.columns(2)
    with c1:
        metric_card(st, "观察池股票", f"{len(watch)} 只", "跨批次累积", "")
    with c2:
        with_action = int((watch["suggested_action"].notna() & (watch["suggested_action"] != "仅观察")).sum())
        metric_card(st, "有交易建议", f"{with_action} 只", "含买入/止损/止盈", "active" if with_action > 0 else "")

    display_cols = [
        "code", "name", "industry", "suggested_action", "watch_price", "trigger_price",
        "buy_lower", "buy_upper", "suggested_buy_price", "stop_loss_price",
        "take_profit_1", "take_profit_2", "suggested_position", "added_at"
    ]
    st.dataframe(watch[display_cols], use_container_width=True, hide_index=True,
                 column_config={
                     "suggested_action": "建议动作",
                     "watch_price": "观察价",
                     "trigger_price": "触发价",
                     "buy_lower": "买入下限",
                     "buy_upper": "买入上限",
                     "suggested_buy_price": "建议买入价",
                     "stop_loss_price": "止损价",
                     "take_profit_1": "止盈1",
                     "take_profit_2": "止盈2",
                     "suggested_position": "仓位%",
                     "added_at": "加入时间",
                 })

    # Export
    csv = watch.to_csv(index=False).encode("utf-8")
    st.download_button("⭳ 导出观察池 CSV", csv,
                      f"watch_pool_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                      "text/csv", use_container_width=True)


def render_run_log(st, conn) -> None:
    logs = read_sql(conn, "SELECT * FROM run_log ORDER BY run_timestamp DESC LIMIT 50")
    render_section(st, "Run Log", "策略运行日志")

    # Summary stats
    if not logs.empty:
        latest = logs.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        with c1: metric_card(st, "总运行次数", f"{len(logs)} 次", "", "")
        with c2: metric_card(st, "最近候选", f"{int(latest['candidate_count'])} 只", str(latest['trade_date']), "active" if int(latest['candidate_count']) > 0 else "")
        with c3: metric_card(st, "最近剔除", f"{int(latest['excluded_count'])} 只", str(latest['trade_date']), "")
        with c4: metric_card(st, "最近状态", str(latest['status']), str(latest['data_source']), "")

        log_display_cols = [col for col in RUN_LOG_DISPLAY_COLUMNS if col in logs.columns]
        st.dataframe(
            logs[log_display_cols].rename(columns=RUN_LOG_DISPLAY_COLUMNS),
            use_container_width=True,
            hide_index=True,
        )

        # Batch selector for detail
        batch_options = logs["batch_id"].tolist()
        selected = st.selectbox("选择批次查看详情", batch_options)
        _render_funnel(st, conn, selected, expanded=True, show_summary=True)

        results = read_sql(
            conn,
            """
            SELECT code, name, industry, status, signal_status, trend_template_type,
                   keypoint_type, trade_plan_type, suggested_action, suggested_position,
                   include_reason, exclude_reason, risk_warning
            FROM strategy_result
            WHERE batch_id = ?
            ORDER BY status, code
            """,
            (selected,),
        )
        tab1, tab2 = st.tabs(["候选股", "剔除股"])
        with tab1:
            included = results[results["status"] == "included"]
            if included.empty:
                st.caption("本批次无候选股")
            else:
                st.dataframe(
                    included.rename(
                        columns={
                            "code": "代码",
                            "name": "名称",
                            "industry": "行业",
                            "status": "状态",
                            "signal_status": "信号状态",
                            "trend_template_type": "趋势类型",
                            "keypoint_type": "关键点类型",
                            "trade_plan_type": "计划类型",
                            "suggested_action": "建议动作",
                            "suggested_position": "建议仓位",
                            "include_reason": "纳入原因",
                            "exclude_reason": "剔除原因",
                            "risk_warning": "风险提示",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
        with tab2:
            excluded = results[results["status"] == "excluded"]
            display_excluded = excluded.rename(
                columns={
                    "code": "代码",
                    "name": "名称",
                    "industry": "行业",
                    "status": "状态",
                    "signal_status": "信号状态",
                    "trend_template_type": "趋势类型",
                    "keypoint_type": "关键点类型",
                    "trade_plan_type": "计划类型",
                    "suggested_action": "建议动作",
                    "suggested_position": "建议仓位",
                    "include_reason": "纳入原因",
                    "exclude_reason": "剔除原因",
                    "risk_warning": "风险提示",
                }
            )
            st.dataframe(display_excluded, use_container_width=True, hide_index=True)
