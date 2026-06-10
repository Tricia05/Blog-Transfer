"""Streamlit UI: scrape blog posts -> editable preview -> CSV/XLSX -> WordPress.

Three pages, switched via the sidebar:
  - Importer  : scan a site, edit & export, deploy to WordPress
  - History   : browse past scans, reload them into Importer
  - Exports   : list of files saved on disk

Background scans run in a thread. Top-bar Stop/Pause buttons drive the
scan via threading events; Deploy uploads the current dataframe to a
WordPress site using the existing migrator client.
"""
from __future__ import annotations

import io
import time

import pandas as pd
import streamlit as st

from scraper import storage
from scraper.runner import start_scan, ScanState, COLUMNS
from migrator.wordpress import WordPressClient, WordPressError


STATUS_OPTIONS = ["publish", "draft", "pending", "scheduled"]
WP_STATUS_MAP = {  # UI label -> WP REST API value
    "publish": "publish",      # already the API value
    "published": "publish",    # legacy value from older scans
    "draft": "draft",
    "pending": "pending",
    "scheduled": "future",
}


def _migrate_legacy_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Bring old saved scans up to current schema."""
    # Old: "Published" column -> blog_status
    if "Published" in df.columns and "blog_status" not in df.columns:
        df = df.rename(columns={"Published": "blog_status"})
    # Old: "published" -> "publish"
    if "blog_status" in df.columns:
        df["blog_status"] = df["blog_status"].replace({"published": "publish"})
    # Old: separate blog_dates + blog_time -> merged blog_dates
    if "blog_time" in df.columns:
        if "blog_dates" in df.columns:
            df["blog_dates"] = (
                df["blog_dates"].fillna("").astype(str).str.strip()
                + " "
                + df["blog_time"].fillna("").astype(str).str.strip()
            ).str.strip()
        df = df.drop(columns=["blog_time"])
    return df


def build_xlsx(df: pd.DataFrame) -> bytes:
    """Write the dataframe to XLSX with a real Excel date+time cell.

    blog_dates -> Excel datetime, formatted as 'March 14, 2012 1:30 PM'
    """
    out = df.copy()
    if "blog_dates" in out.columns:
        out["blog_dates"] = pd.to_datetime(out["blog_dates"], errors="coerce")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out.to_excel(writer, index=False, sheet_name="Posts")
        ws = writer.sheets["Posts"]
        headers = {cell.value: cell.column_letter for cell in ws[1]}
        if "blog_dates" in headers:
            for cell in ws[headers["blog_dates"]][1:]:
                cell.number_format = "mmmm d, yyyy h:mm AM/PM"
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Page config + styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Blog Importer", layout="wide",
    initial_sidebar_state="expanded", page_icon="📥",
)

CSS = """
<style>
.stApp { background:#0B0E14; }
section[data-testid="stSidebar"] { background:#0E121B; border-right:1px solid #1E2330; }
section[data-testid="stSidebar"] .nav-logo {
    display:flex; align-items:center; gap:.6rem;
    padding:.6rem .25rem 1.2rem .25rem;
    font-weight:700; font-size:1.05rem; color:#E6E8EE;
}
section[data-testid="stSidebar"] .nav-logo .logo-box {
    width:32px; height:32px; border-radius:8px;
    background:linear-gradient(135deg,#7C5CFF,#5B3FE5);
    display:flex; align-items:center; justify-content:center; color:#fff;
}
section[data-testid="stSidebar"] .stButton > button {
    width:100%; text-align:left; justify-content:flex-start;
    background:transparent; border:0; color:#A6ADBB;
    padding:.55rem .75rem; font-size:.92rem; border-radius:8px; margin:0;
}
section[data-testid="stSidebar"] .stButton > button:hover { background:#1A1F2C; color:#E6E8EE; }
section[data-testid="stSidebar"] .nav-active .stButton > button {
    background:#1A1F2C; color:#E6E8EE; font-weight:600;
}

.page-title { font-size:1.9rem; font-weight:700; margin:.3rem 0 .25rem 0; }
.page-sub   { color:#8A93A6; margin-bottom:1.5rem; }

.step-card {
    background:#0F1320; border:1px solid #1E2330; border-radius:14px;
    padding:1.25rem 1.4rem 1.4rem 1.4rem; margin-bottom:1.1rem;
}
.step-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem; }
.step-title { display:flex; align-items:center; gap:.7rem; font-weight:600; font-size:1.05rem; }
.step-num {
    width:28px; height:28px; border-radius:7px;
    background:rgba(124,92,255,.18); color:#A48BFF;
    display:inline-flex; align-items:center; justify-content:center;
    font-weight:700; font-size:.85rem;
}

[data-baseweb="tag"][aria-label*="publish"]   { background:#16382A !important; color:#3DDC97 !important; }
[data-baseweb="tag"][aria-label*="draft"]     { background:#2A2F3B !important; color:#C9CDD8 !important; }
[data-baseweb="tag"][aria-label*="pending"]   { background:#3A2C12 !important; color:#F5B14E !important; }
[data-baseweb="tag"][aria-label*="scheduled"] { background:#15324A !important; color:#5BB8FF !important; }

.empty {
    border:1px dashed #2A3040; border-radius:12px; padding:3rem 1rem;
    text-align:center; color:#8A93A6;
}
.empty .icon-circle {
    width:64px; height:64px; border-radius:50%;
    background:#1A1F2C; display:inline-flex; align-items:center; justify-content:center;
    font-size:1.6rem; margin-bottom:.7rem;
}

.row-card .row-meta { color:#8A93A6; font-size:.85rem; }
.row-card .row-title { color:#E6E8EE; font-weight:600; }

.circ {
    --p:0; width:120px; height:120px; border-radius:50%;
    background:conic-gradient(#7C5CFF calc(var(--p)*1%), #1E2330 0);
    display:inline-flex; align-items:center; justify-content:center; position:relative;
}
.circ::after { content:""; position:absolute; inset:8px; background:#0F1320; border-radius:50%; }
.circ .circ-text { position:relative; z-index:1; text-align:center; }
.circ .circ-text .big { font-size:1.45rem; font-weight:700; color:#E6E8EE; }
.circ .circ-text .small { font-size:.78rem; color:#8A93A6; }

/* Hide Streamlit's running indicator + its own Stop button, but keep
   the Deploy button visible. */
#MainMenu { visibility: hidden; }
[data-testid="stStatusWidget"] { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; }

/* Hide Streamlit Cloud's bottom-right host badge / Manage app button */
.viewerBadge_container__1QSob,
[class*="viewerBadge_container"],
[class*="profileContainer"],
[class*="styles_terminalButton"],
[data-testid="stToolbarAvatar"],
[data-testid="stHostingMenu"],
[data-testid="stAppDeployButton"] { display: none !important; }
footer { visibility: hidden !important; }

/* Move any toast notifications below the topbar buttons */
div[data-testid="stToastContainer"],
[data-testid="stToast"] {
    top: 80px !important;
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def _init_state() -> None:
    defaults = {
        "page": "importer",
        "df": pd.DataFrame(columns=COLUMNS),
        "last_url": "",
        "scan_state": None,        # ScanState or None
        "scan_persisted": False,   # have we saved this scan to history yet?
        "show_deploy": False,
        "deploy_log": [],          # list of {"ok": bool, "msg": str}
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


def go(page: str) -> None:
    st.session_state.page = page


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        """<div class="nav-logo">
            <div class="logo-box">📥</div>
            <div>Blog Importer</div>
        </div>""",
        unsafe_allow_html=True,
    )

    def nav(key: str, label: str, icon: str) -> None:
        active = st.session_state.page == key
        st.markdown('<div class="nav-active">' if active else "<div>", unsafe_allow_html=True)
        if st.button(f"{icon}  {label}", key=f"nav_{key}"):
            go(key); st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    nav("importer", "Importer", "🔍")
    nav("history",  "History",  "🕘")
    nav("exports",  "Exports",  "⬇️")


# ---------------------------------------------------------------------------
# Top action bar — Stop / Pause / Deploy
# ---------------------------------------------------------------------------
def render_topbar() -> None:
    state: ScanState | None = st.session_state.scan_state
    spacer, col_stop, col_pause, col_deploy = st.columns([7, 1.1, 1.3, 1.4])

    with col_stop:
        running = state is not None and state.is_running()
        if st.button("⏹  Stop", disabled=not running, use_container_width=True):
            if state:
                state.request_stop()

    with col_pause:
        running = state is not None and state.is_running()
        paused = state is not None and state.is_paused()
        label = "▶  Resume" if paused else "⏸  Pause"
        if st.button(label, disabled=not running, use_container_width=True):
            if state:
                state.toggle_pause()

    with col_deploy:
        df_ready = not st.session_state.df.empty
        if st.button("🚀  Deploy", disabled=not df_ready, type="primary",
                     use_container_width=True):
            st.session_state.show_deploy = True
            st.rerun()


render_topbar()


# ===========================================================================
# IMPORTER PAGE
# ===========================================================================
def render_progress_card(state: ScanState) -> None:
    p = state.progress
    found = p.get("found", 0)
    done = p.get("done", 0)
    pct = int(done / found * 100) if found else 0
    phase = p.get("phase", "idle")
    msg = p.get("message", "")
    cur = p.get("current_url", "")
    paused_badge = (
        '<span style="background:#3A2C12;color:#F5B14E;padding:.2rem .55rem;'
        'border-radius:6px;font-size:.78rem;margin-left:.5rem;">PAUSED</span>'
        if state.is_paused() else ""
    )
    st.markdown(
        f"""<div class="step-card">
            <div class="step-title" style="margin-bottom:1rem;">
                <div class="step-num">2</div> Scanning Progress {paused_badge}
            </div>
            <div style="display:flex; gap:1.6rem; align-items:center;">
                <div class="circ" style="--p:{pct};">
                    <div class="circ-text">
                        <div class="big">{done}</div>
                        <div class="small">of {found}</div>
                    </div>
                </div>
                <div style="flex:1;">
                    <div style="font-size:1.15rem; font-weight:600; margin-bottom:.4rem;">
                        Found {found} post URLs
                    </div>
                    <div style="color:#8A93A6; font-size:.9rem; margin-bottom:.6rem; word-break:break-all;">
                        {done}/{found} &nbsp;•&nbsp; {cur}
                    </div>
                    <div style="background:#1E2330; border-radius:6px; height:8px; overflow:hidden;">
                        <div style="background:linear-gradient(90deg,#7C5CFF,#5B3FE5);
                            width:{pct}%; height:100%;"></div>
                    </div>
                    <div style="color:#8A93A6; font-size:.85rem; margin-top:.6rem;">
                        Phase: <b>{phase}</b> &nbsp;•&nbsp; {msg}
                    </div>
                </div>
                <div style="font-size:1.05rem; font-weight:600; color:#A48BFF;">{pct}%</div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_importer() -> None:
    st.markdown(
        """<div class="page-title">Blog Importer</div>
        <div class="page-sub">Enter a blog or website URL, scan the posts, edit any field in the table, then export as CSV/XLSX or deploy directly to WordPress.</div>""",
        unsafe_allow_html=True,
    )

    state: ScanState | None = st.session_state.scan_state

    # ---- Step 1: Source & Scan ----
    st.markdown(
        """<div class="step-card">
            <div class="step-head">
                <div class="step-title"><div class="step-num">1</div> Source &amp; Scan</div>
            </div>""",
        unsafe_allow_html=True,
    )
    scan_all = st.checkbox("Scan all posts", value=True, key="scan_all_cb")
    with st.form("scan_form", clear_on_submit=False):
        c1, c2 = st.columns([4, 1])
        with c1:
            url = st.text_input(
                "Website or blog listing URL",
                value=st.session_state.last_url,
                placeholder="https://example.com/blog/",
            )
            st.caption(
                "Enter the homepage or blog listing page. Every scraped post is "
                "marked as **publish** to match the source site — you can change "
                "individual rows (or bulk-change everything to draft) after scanning."
            )
        with c2:
            limit = st.number_input(
                "Limit (posts)", min_value=1, max_value=100000, value=10,
                disabled=scan_all,
            )
        scan_clicked = st.form_submit_button(
            "🔍  Scan", type="primary",
            disabled=(state is not None and state.is_running()),
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # Status applied to scanned posts (publish = mirror source)
    default_status: list[str] = ["publish"]

    # ---- Kick off a new scan ----
    if scan_clicked:
        if not url.strip():
            st.error("Please enter a URL.")
        elif state is not None and state.is_running():
            st.warning("A scan is already running.")
        else:
            st.session_state.last_url = url.strip()
            st.session_state.scan_persisted = False
            st.session_state.scan_state = start_scan(
                url.strip(),
                0 if scan_all else int(limit),
                default_status,
            )
            st.rerun()

    # ---- Step 2: live progress while scan thread runs ----
    state = st.session_state.scan_state
    if state is not None and (state.is_running() or state.progress["phase"] != "idle"):
        render_progress_card(state)

        # When the worker finishes, copy result into the dataframe and persist
        if not state.is_running() and state.result_df is not None and not st.session_state.scan_persisted:
            st.session_state.df = state.result_df
            if not state.result_df.empty:
                entry = storage.save_history(
                    st.session_state.last_url,
                    state.result_df,
                    default_status,
                )
                st.success(f"Scan complete — saved to history: {entry.id} ({len(state.result_df)} posts)")
            st.session_state.scan_persisted = True

        # Auto-refresh while running so the progress card updates
        if state.is_running():
            time.sleep(0.6)
            st.rerun()

    # ---- Step 3: Preview Results ----
    df = st.session_state.df
    st.markdown(
        """<div class="step-card">
            <div class="step-head">
                <div class="step-title"><div class="step-num">3</div> Preview Results</div>
            </div>""",
        unsafe_allow_html=True,
    )
    if df.empty:
        st.markdown(
            """<div class="empty">
                <div class="icon-circle">📄</div>
                <div style="font-size:1.05rem; font-weight:600; color:#E6E8EE;">No data yet</div>
                <div>Enter a URL above and click Scan to discover posts.</div>
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        bc1, bc2, bc3 = st.columns([2, 1, 5])
        with bc1:
            bulk_status = st.selectbox(
                "Set status for ALL rows", STATUS_OPTIONS, index=0, key="bulk_status",
            )
        with bc2:
            st.write(""); st.write("")
            if st.button("Apply to all", use_container_width=True):
                st.session_state.df["blog_status"] = bulk_status
                st.rerun()
        with bc3:
            st.write(""); st.write("")
            st.caption(f"{len(df)} rows. Click any cell to edit.")

        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True,
            column_config={
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "Title": st.column_config.TextColumn("Title", width="medium"),
                "Permalink": st.column_config.LinkColumn("Permalink"),
                "blog_dates": st.column_config.TextColumn(
                    "blog_dates", help="Month D, YYYY H:MM AM/PM",
                ),
                "blog_Category": st.column_config.TextColumn("blog_Category"),
                "blog_Tag": st.column_config.TextColumn("blog_Tag"),
                "blog_featured_image": st.column_config.LinkColumn("blog_featured_image"),
                "blog_content": st.column_config.TextColumn("blog_content", width="large"),
                "blog_metadesc": st.column_config.TextColumn("blog_metadesc", width="medium"),
                "blog_metatitle": st.column_config.TextColumn("blog_metatitle"),
                "blog_status": st.column_config.SelectboxColumn(
                    "blog_status", options=STATUS_OPTIONS, required=True,
                ),
            },
            key="data_editor",
        )
        st.session_state.df = edited

        e1, e2, e3 = st.columns([1, 1, 5])
        csv_bytes = edited.to_csv(index=False).encode("utf-8-sig")
        if e1.download_button(
            "⬇  Export CSV", data=csv_bytes,
            file_name="blog_posts.csv", mime="text/csv",
            use_container_width=True, key="dl_csv",
        ):
            storage.save_export("blog_posts", csv_bytes, "csv")

        xlsx_bytes = build_xlsx(edited)
        if e2.download_button(
            "⬇  Export XLSX", data=xlsx_bytes,
            file_name="blog_posts.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, key="dl_xlsx",
        ):
            storage.save_export("blog_posts", xlsx_bytes, "xlsx")
        e3.caption(f"{len(edited)} rows ready for export. Files are also saved to **Exports**.")

    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# DEPLOY DIALOG (modal-ish via expander)
# ---------------------------------------------------------------------------
def render_deploy_dialog() -> None:
    if not st.session_state.show_deploy:
        return
    df: pd.DataFrame = st.session_state.df
    with st.container(border=True):
        st.markdown("### 🚀 Deploy to WordPress")
        st.caption(
            f"Will create {len(df)} posts on the destination WordPress site. "
            "Use an Application Password (WP Admin → Users → Profile → Application Passwords)."
        )
        colA, colB = st.columns(2)
        wp_url = colA.text_input("WordPress site URL", placeholder="https://example.com")
        wp_user = colB.text_input("WP username")
        wp_pass = st.text_input("Application password", type="password")
        on_dup = st.selectbox(
            "If a post with the same slug already exists",
            ["skip", "update", "create"], index=0,
        )

        c1, c2, c3 = st.columns([1, 1, 4])
        deploy_now = c1.button("Deploy now", type="primary", use_container_width=True)
        cancel = c2.button("Cancel", use_container_width=True)

        if cancel:
            st.session_state.show_deploy = False
            st.rerun()

        if deploy_now:
            if not (wp_url and wp_user and wp_pass):
                st.error("URL, username, and application password are required.")
                return
            try:
                client = WordPressClient(wp_url, wp_user, wp_pass)
                me = client.verify()
                st.success(f"Connected as {me.get('name')} (id {me.get('id')}).")
            except (WordPressError, Exception) as e:
                st.error(f"Authentication failed: {e}")
                return

            log: list[dict] = []
            placeholder = st.empty()
            progress = st.progress(0.0)

            for i, row in df.iterrows():
                metadesc = str(row.get("blog_metadesc", "") or "").strip()
                metatitle = str(row.get("blog_metatitle", "") or "").strip()
                payload = {
                    "title": row.get("Title", ""),
                    "content": row.get("blog_content", ""),
                    "excerpt": metadesc,
                    "status": WP_STATUS_MAP.get(row.get("blog_status", "draft"), "draft"),
                    "_categories": [c.strip() for c in str(row.get("blog_Category", "")).split(",") if c.strip()],
                    "_tags": [t.strip() for t in str(row.get("blog_Tag", "")).split(",") if t.strip()],
                }
                # Yoast SEO meta (only sent if the source had a value)
                meta = {}
                if metadesc:
                    meta["_yoast_wpseo_metadesc"] = metadesc
                if metatitle:
                    meta["_yoast_wpseo_title"] = metatitle
                if meta:
                    payload["meta"] = meta
                # Date + time (send both 'date' and 'date_gmt' so WordPress
                # doesn't shift the displayed time based on site timezone)
                date_str = str(row.get("blog_dates", "")).strip()
                if date_str:
                    dt = pd.to_datetime(date_str, errors="coerce")
                    if pd.notna(dt):
                        iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        payload["date"] = iso
                        payload["date_gmt"] = iso
                # Featured image
                fi = str(row.get("blog_featured_image", "")).strip()
                if fi:
                    payload["_featured_image_url"] = fi

                title = payload["title"] or row.get("Permalink", "")
                date_sent = payload.get("date", "(no date)")
                try:
                    result = client.upload_post(payload, on_duplicate=on_dup)
                    note = result.get("note", "")
                    extra = f"  [{note}]" if note else ""
                    log.append({
                        "ok": True,
                        "msg": f"{result['action']} #{result.get('id')} — {title}  [date: {date_sent}]{extra}",
                    })
                except WordPressError as e:
                    log.append({"ok": False, "msg": f"FAIL — {title}: {e}"})

                progress.progress((i + 1) / max(1, len(df)))
                # Print last few lines live
                placeholder.markdown(
                    "\n".join(
                        f"{'✅' if x['ok'] else '❌'} {x['msg']}" for x in log[-8:]
                    )
                )

            st.session_state.deploy_log = log
            ok = sum(1 for x in log if x["ok"])
            fail = len(log) - ok
            st.success(f"Deploy finished — {ok} ok, {fail} failed.")


# ===========================================================================
# HISTORY PAGE
# ===========================================================================
def render_history() -> None:
    st.markdown(
        """<div class="page-title">History</div>
        <div class="page-sub">Every scan you run is saved here. Click <b>Load</b> to bring it back into the Importer for editing or re-export.</div>""",
        unsafe_allow_html=True,
    )
    entries = storage.list_history()
    if not entries:
        st.markdown(
            """<div class="empty">
                <div class="icon-circle">🕘</div>
                <div style="font-size:1.05rem; font-weight:600; color:#E6E8EE;">No scans yet</div>
                <div>Run a scan in the Importer and it will appear here.</div>
            </div>""",
            unsafe_allow_html=True,
        )
        return

    for e in entries:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([4, 2, 1.2, 1.2])
            with c1:
                st.markdown(
                    f"<div class='row-title'>{e.url}</div>"
                    f"<div class='row-meta'>{e.display_time} &nbsp;•&nbsp; "
                    f"{e.post_count} posts &nbsp;•&nbsp; statuses: {', '.join(e.statuses)}</div>",
                    unsafe_allow_html=True,
                )
            with c2:
                st.caption(f"ID: `{e.id}`")
            with c3:
                if st.button("Load", key=f"load_{e.id}", use_container_width=True):
                    df = storage.load_history(e.id)
                    if df is not None:
                        df = _migrate_legacy_columns(df)
                        for col in COLUMNS:
                            if col not in df.columns:
                                df[col] = ""
                        st.session_state.df = df[COLUMNS]
                        st.session_state.last_url = e.url
                        st.session_state.page = "importer"
                        st.rerun()
            with c4:
                if st.button("Delete", key=f"del_{e.id}", use_container_width=True):
                    storage.delete_history(e.id)
                    st.rerun()


# ===========================================================================
# EXPORTS PAGE
# ===========================================================================
def render_exports() -> None:
    st.markdown(
        """<div class="page-title">Exports</div>
        <div class="page-sub">Files you exported are saved on disk. Re-download or remove any below.</div>""",
        unsafe_allow_html=True,
    )
    entries = storage.list_exports()
    if not entries:
        st.markdown(
            """<div class="empty">
                <div class="icon-circle">⬇️</div>
                <div style="font-size:1.05rem; font-weight:600; color:#E6E8EE;">No exports yet</div>
                <div>Export a CSV or XLSX from the Importer and it will appear here.</div>
            </div>""",
            unsafe_allow_html=True,
        )
        return

    for e in entries:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([4, 2, 1.2, 1.2])
            with c1:
                st.markdown(
                    f"<div class='row-title'>{e.filename}</div>"
                    f"<div class='row-meta'>{e.display_time} &nbsp;•&nbsp; "
                    f"{e.size_kb} &nbsp;•&nbsp; {e.fmt.upper()}</div>",
                    unsafe_allow_html=True,
                )
            with c2:
                st.caption(f"Format: {e.fmt}")
            with c3:
                path = storage.export_path(e.filename)
                if path.exists():
                    mime = "text/csv" if e.fmt == "csv" else (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    st.download_button(
                        "Download", data=path.read_bytes(),
                        file_name=e.filename, mime=mime,
                        key=f"dl_{e.filename}", use_container_width=True,
                    )
            with c4:
                if st.button("Delete", key=f"delx_{e.filename}", use_container_width=True):
                    storage.delete_export(e.filename)
                    st.rerun()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
page = st.session_state.page
if page == "importer":
    render_importer()
    render_deploy_dialog()
elif page == "history":
    render_history()
elif page == "exports":
    render_exports()
