"""
CSP Screener Dashboard — Streamlit web app.

Reads directly from the JSONL journals. No DB, no sync, no extra infra.

Run with:
    streamlit run csp_screener/dashboard/app.py

Open in browser at http://localhost:8501

For phone access from anywhere, see Cloudflare Tunnel setup in README.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make the csp_screener package importable when run with `streamlit run`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from csp_screener import config, evaluator, journal, virtual_tracker


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="CSP Screener Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Cache reads — the journal grows slowly, so 60s is plenty fresh.
@st.cache_data(ttl=60)
def load_screens():
    return journal.read_all("screens")


@st.cache_data(ttl=60)
def load_virtual_events():
    return journal.read_all("virtual_trades")


@st.cache_data(ttl=60)
def load_system_events():
    return journal.read_all("system_events")


@st.cache_data(ttl=60)
def compute_summaries():
    return evaluator.all_periods_summary()


# ---------------------------------------------------------------------------
# Sidebar — navigation
# ---------------------------------------------------------------------------
st.sidebar.title("CSP Screener")
st.sidebar.caption(
    "Reading from local JSONL journal.\n"
    "No real money traded — virtual track record only."
)

page = st.sidebar.radio(
    "Pages",
    ["📊 Dashboard", "📥 Latest screen", "📈 Virtual portfolio",
     "🎯 Track record", "🛠️ System health", "⚙️ Configuration"],
)

st.sidebar.divider()
if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Generated {datetime.now().strftime('%H:%M:%S')}")


# ---------------------------------------------------------------------------
# Page: Dashboard
# ---------------------------------------------------------------------------
def page_dashboard():
    st.title("📊 Dashboard")
    st.caption("Quick overview of screener state and performance.")

    screens = load_screens()
    virtual_events = load_virtual_events()
    summaries = compute_summaries()

    # Top metrics row
    open_trades = virtual_tracker.get_open_virtual_trades()
    all_time = summaries["all"]
    last_30 = summaries["30d"]

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total screens run", len(screens))
    with c2:
        st.metric("Open virtual positions", len(open_trades))
    with c3:
        st.metric(
            "Closed virtual trades",
            all_time.closed_count,
            delta=f"{last_30.closed_count} last 30d" if last_30.closed_count else None,
        )
    with c4:
        st.metric(
            "All-time virtual P&L",
            f"${all_time.total_pnl:+.2f}",
            delta=f"${last_30.total_pnl:+.2f} last 30d" if last_30.closed_count else None,
        )

    st.divider()

    # Latest screen snapshot
    if screens:
        latest = screens[-1]
        st.subheader(f"Latest screen — {latest.get('ran_at', '?')[:10]}")
        scol1, scol2, scol3, scol4 = st.columns(4)
        with scol1:
            st.metric("Universe", latest.get("universe_size", 0))
        with scol2:
            st.metric("Passed filters", latest.get("passed_filters", 0))
        with scol3:
            st.metric("Candidates emailed", latest.get("candidates_in_email", 0))
        with scol4:
            vix = latest.get("vix")
            st.metric("VIX", f"{vix:.1f}" if vix else "—")
        if latest.get("tickers_in_email"):
            st.write(
                "**Candidates in last email:** "
                + ", ".join(f"`{t}`" for t in latest["tickers_in_email"])
            )

    st.divider()

    # Cumulative virtual P&L chart
    closed_trades = _reconstruct_closed_trades(virtual_events)
    if closed_trades:
        st.subheader("Cumulative virtual P&L")
        df = pd.DataFrame(closed_trades)
        df["closed_at"] = pd.to_datetime(df["closed_at"])
        df = df.sort_values("closed_at")
        df["cumulative_pnl"] = df["pnl"].cumsum()

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df["closed_at"], y=df["cumulative_pnl"],
            mode="lines+markers",
            line=dict(color="#2da44e", width=2),
            marker=dict(size=6),
            fill="tozeroy",
            fillcolor="rgba(45,164,78,0.1)",
        ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(
            xaxis_title="", yaxis_title="Cumulative P&L ($)",
            height=350, margin=dict(t=20, b=20),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(
            "📊 No closed virtual trades yet. The screener will start "
            "showing performance data after the first virtual position closes."
        )

    # Open positions snapshot
    st.subheader("Open virtual positions")
    if open_trades:
        rows = []
        for t in open_trades:
            rows.append({
                "Ticker": t.ticker,
                "Strike": f"${t.strike:.2f}",
                "Expiration": t.expiration.date().isoformat(),
                "Credit": f"${t.credit_received:.2f}",
                "Max loss": f"${t.max_loss:.2f}",
                "Opened": t.opened_at.date().isoformat(),
                "DTE remaining": (t.expiration.date() - datetime.now().date()).days,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.write("_None._")


# ---------------------------------------------------------------------------
# Page: Latest screen
# ---------------------------------------------------------------------------
def page_latest_screen():
    st.title("📥 Latest screen")
    screens = load_screens()
    if not screens:
        st.warning("No screens recorded yet. Run `python -m csp_screener.main` first.")
        return

    latest = screens[-1]
    ran_at = latest.get("ran_at", "?")
    st.caption(f"Generated {ran_at}")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Tickers in email", latest.get("candidates_in_email", 0))
    with c2:
        st.metric("Positions opened", latest.get("virtual_positions_opened", 0))
    with c3:
        st.metric(
            "Closed this run",
            latest.get("virtual_closed_this_run", 0),
            delta=f"${latest.get('virtual_closed_pnl', 0):+.2f}" if latest.get('virtual_closed_pnl') else None,
        )

    tickers = latest.get("tickers_in_email", [])
    if tickers:
        st.subheader("Tickers in latest email")
        st.write(", ".join(f"`{t}`" for t in tickers))

    # All historical screens
    st.subheader("Past screens")
    df = pd.DataFrame([
        {
            "Date": s.get("ran_at", "")[:10],
            "Universe": s.get("universe_size", 0),
            "Passed": s.get("passed_filters", 0),
            "Sent": s.get("candidates_in_email", 0),
            "Opened": s.get("virtual_positions_opened", 0),
            "Closed": s.get("virtual_closed_this_run", 0),
            "Closed PnL": s.get("virtual_closed_pnl", 0),
            "VIX": s.get("vix"),
            "Email sent": s.get("email_sent", False),
        }
        for s in reversed(screens[-30:])
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page: Virtual portfolio
# ---------------------------------------------------------------------------
def page_virtual_portfolio():
    st.title("📈 Virtual portfolio")
    st.caption(
        "Mark-to-market of currently open virtual positions. "
        "Updated each daily check + weekly screen."
    )

    open_trades = virtual_tracker.get_open_virtual_trades()
    if not open_trades:
        st.info("No open virtual positions.")
        return

    # Build display table
    rows = []
    total_credit = 0
    total_max_loss = 0
    for t in open_trades:
        dte_remaining = max(0, (t.expiration.date() - datetime.now().date()).days)
        days_held = (datetime.now().date() - t.opened_at.date()).days
        total_credit += t.credit_received
        total_max_loss += t.max_loss
        rows.append({
            "Ticker": t.ticker,
            "Strike": t.strike,
            "Expiration": t.expiration.date().isoformat(),
            "DTE remaining": dte_remaining,
            "Days held": days_held,
            "Credit received": t.credit_received,
            "Max loss": t.max_loss,
            "Breakeven": t.breakeven,
            "Spot at open": t.spot_at_open,
            "Opened on": t.opened_at.date().isoformat(),
        })

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Open positions", len(open_trades))
    with c2:
        st.metric("Total credit at risk", f"${total_credit:.2f}")
    with c3:
        st.metric("Total max loss exposure", f"${total_max_loss:.2f}")

    df = pd.DataFrame(rows).sort_values("DTE remaining")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Strike": st.column_config.NumberColumn(format="$%.2f"),
            "Credit received": st.column_config.NumberColumn(format="$%.2f"),
            "Max loss": st.column_config.NumberColumn(format="$%.2f"),
            "Breakeven": st.column_config.NumberColumn(format="$%.2f"),
            "Spot at open": st.column_config.NumberColumn(format="$%.2f"),
        },
    )


# ---------------------------------------------------------------------------
# Page: Track record
# ---------------------------------------------------------------------------
def page_track_record():
    st.title("🎯 Virtual track record")
    st.caption(
        "What would the screener have made if you'd taken every weekly "
        "suggestion? Pure paper trading — no real money."
    )

    summaries = compute_summaries()

    # 3 period cards
    cols = st.columns(3)
    for i, key in enumerate(["30d", "90d", "all"]):
        s = summaries[key]
        with cols[i]:
            st.markdown(f"**{s.period_label.upper()}**")
            if s.closed_count == 0:
                st.caption("No closed trades in this period yet.")
                continue
            st.metric(
                "Net P&L", f"${s.total_pnl:+.2f}",
                delta=f"{s.win_rate*100:.0f}% win rate",
            )
            sub1, sub2 = st.columns(2)
            with sub1:
                st.caption(f"Trades: **{s.closed_count}**")
                st.caption(f"Avg/trade: ${s.avg_pnl:+.2f}")
                st.caption(f"PF: {s.profit_factor:.2f}")
            with sub2:
                st.caption(f"Best: ${s.best_trade:+.2f}")
                st.caption(f"Worst: ${s.worst_trade:+.2f}")
                st.caption(f"Expectancy: ${s.expectancy:+.2f}")

    st.divider()

    # Closed trades table
    virtual_events = load_virtual_events()
    closed_trades = _reconstruct_closed_trades(virtual_events)
    if not closed_trades:
        st.info("No closed virtual trades yet.")
        return

    st.subheader("Closed virtual trades")
    df = pd.DataFrame(closed_trades)
    df["closed_at_str"] = pd.to_datetime(df["closed_at"]).dt.strftime("%Y-%m-%d")
    df["opened_at_str"] = pd.to_datetime(df["opened_at"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("closed_at", ascending=False)

    show_df = df[[
        "ticker", "strike", "opened_at_str", "closed_at_str",
        "exit_reason", "pnl", "pnl_pct_of_credit",
    ]].copy()
    show_df.columns = ["Ticker", "Strike", "Opened", "Closed", "Reason", "P&L", "% of credit"]
    st.dataframe(
        show_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Strike": st.column_config.NumberColumn(format="$%.2f"),
            "P&L": st.column_config.NumberColumn(format="$%+.2f"),
            "% of credit": st.column_config.NumberColumn(format="%.1%"),
        },
    )

    # Charts
    st.divider()
    cc1, cc2 = st.columns(2)
    with cc1:
        st.subheader("By exit reason")
        all_summary = summaries["all"]
        if all_summary.by_exit_reason:
            reasons_df = pd.DataFrame([
                {"Reason": k, "Count": v["count"], "PnL": v["pnl"]}
                for k, v in all_summary.by_exit_reason.items()
            ])
            fig = px.bar(
                reasons_df, x="Reason", y="PnL", color="Count",
                color_continuous_scale="Blues",
                text="Count",
            )
            fig.update_layout(height=320, margin=dict(t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)

    with cc2:
        st.subheader("By ticker")
        if all_summary.by_ticker:
            tk_df = pd.DataFrame([
                {"Ticker": k, "Trades": v["trades"], "Win rate": v["win_rate"], "PnL": v["pnl"]}
                for k, v in all_summary.by_ticker.items()
            ]).sort_values("PnL", ascending=False).head(15)
            fig = px.bar(
                tk_df, x="Ticker", y="PnL", color="Win rate",
                color_continuous_scale="RdYlGn",
                text="Trades",
            )
            fig.update_layout(height=320, margin=dict(t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: System health
# ---------------------------------------------------------------------------
def page_system_health():
    st.title("🛠️ System health")

    screens = load_screens()
    system_events = load_system_events()

    # Last screen freshness
    st.subheader("Liveness")
    if screens:
        last_run = pd.to_datetime(screens[-1].get("ran_at"))
        hours_since = (datetime.now() - last_run.to_pydatetime()).total_seconds() / 3600
        if hours_since < 24 * 8:  # within a week
            st.success(f"✅ Last screener run: {last_run.strftime('%Y-%m-%d %H:%M')} "
                       f"({hours_since:.1f}h ago)")
        else:
            st.error(f"⚠️ Last run was {hours_since/24:.1f} days ago — "
                     f"check the scheduler / watchdog.")
    else:
        st.warning("No screens recorded yet.")

    # System event log
    st.subheader("Recent system events (last 30)")
    if system_events:
        evs = list(reversed(system_events[-30:]))
        df = pd.DataFrame(evs)
        # Reorder columns sensibly
        priority = ["recorded_at", "event", "at", "error"]
        cols = [c for c in priority if c in df.columns] + [c for c in df.columns if c not in priority]
        df = df[cols]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.write("_No system events recorded yet._")

    # Journal file sizes
    st.subheader("Journal files on disk")
    info = []
    for topic, path in journal.JOURNAL_FILES.items():
        size_kb = path.stat().st_size / 1024 if path.exists() else 0
        info.append({
            "Topic": topic,
            "File": str(path),
            "Records": journal.topic_count(topic),
            "Size (KB)": round(size_kb, 1),
        })
    st.dataframe(pd.DataFrame(info), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page: Configuration (read-only view)
# ---------------------------------------------------------------------------
def page_config():
    st.title("⚙️ Configuration")
    st.caption(
        "Read-only view of `config.py`. To change values, edit the file directly "
        "(subject to the 14-day cooldown rule)."
    )

    sections = [
        ("Universe filters", [
            ("Price min", f"${config.PRICE_MIN}"),
            ("Price max", f"${config.PRICE_MAX}"),
            ("Min daily volume", f"{config.MIN_DAILY_VOLUME:,}"),
            ("RV history window", f"{config.RV_HISTORY_DAYS}d"),
            ("RV current window", f"{config.RV_WINDOW_DAYS}d"),
        ]),
        ("Options filters", [
            ("Min open interest", config.MIN_OPEN_INTEREST),
            ("Max bid/ask spread", f"{config.MAX_BID_ASK_PCT_OF_MID:.0%}"),
            ("Target delta", f"{config.TARGET_DELTA:.2f}"),
            ("DTE range", f"{config.DTE_MIN}–{config.DTE_MAX}"),
        ]),
        ("Earnings exclusion", [
            ("Block earnings holds", config.NEVER_HOLD_THROUGH_EARNINGS),
            ("Exclusion window", f"{config.EARNINGS_EXCLUSION_DAYS}d"),
        ]),
        ("Risk limits", [
            ("Max open positions", config.MAX_OPEN_POSITIONS),
            ("Max total premium at risk", f"${config.MAX_TOTAL_PREMIUM_AT_RISK}"),
            ("VIX kill switch", f">{config.VIX_KILL_SWITCH}"),
            ("Drawdown kill switch", f"{config.ACCOUNT_DRAWDOWN_KILL_SWITCH:.0%}"),
        ]),
        ("Virtual exits", [
            ("Take profit", f"{config.VIRTUAL_TP_PCT:.0%} of credit"),
            ("Force exit DTE", f"<= {config.VIRTUAL_FORCE_EXIT_DTE}"),
            ("Stop loss", f"{config.VIRTUAL_SL_MULTIPLE}× credit"),
        ]),
        ("Anti-tinker", [
            ("Threshold change cooldown", f"{config.THRESHOLD_CHANGE_COOLDOWN_DAYS}d"),
        ]),
    ]

    for title, items in sections:
        with st.expander(title, expanded=True):
            for k, v in items:
                st.markdown(f"- **{k}**: `{v}`")


# ---------------------------------------------------------------------------
# Helper — reconstruct closed trades by joining open + close events
# ---------------------------------------------------------------------------
def _reconstruct_closed_trades(events: list[dict]) -> list[dict]:
    opens = {}
    closes = {}
    for ev in events:
        tid = ev.get("trade_id")
        if not tid:
            continue
        if ev.get("event") == "open":
            opens[tid] = ev
        elif ev.get("event") == "close":
            closes[tid] = ev
    out = []
    for tid, close_ev in closes.items():
        open_ev = opens.get(tid)
        if not open_ev:
            continue
        merged = {**open_ev, **close_ev}
        merged["trade_id"] = tid
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
ROUTES = {
    "📊 Dashboard": page_dashboard,
    "📥 Latest screen": page_latest_screen,
    "📈 Virtual portfolio": page_virtual_portfolio,
    "🎯 Track record": page_track_record,
    "🛠️ System health": page_system_health,
    "⚙️ Configuration": page_config,
}

try:
    ROUTES[page]()
except Exception as e:
    st.error(f"Page crashed: {e}")
    st.exception(e)
