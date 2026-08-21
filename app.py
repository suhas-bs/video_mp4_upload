"""
app.py — MP4 → Meta Video Push Tool  (parallel edition)
---------------------------------------------------------
Phased parallel pipeline:
  Phase 1 — Upload all videos simultaneously
  Phase 2 — Poll all videos for ready status simultaneously
  Phase 3 — Create creatives simultaneously
  Phase 4 — Create ads simultaneously

Each phase waits for all videos before moving to the next,
so the live status table stays accurate and readable.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

from drive_helper import (
    cleanup_temp_dir,
    extract_zip_to_temp,
    get_local_path,
    list_mp4_files,
)
from meta_uploader import (
    create_ad,
    create_video_creative,
    poll_video_ready,
    upload_video,
)

st.set_page_config(page_title="MP4 → Meta Video Push", page_icon="🎬", layout="wide")

STATUS_EMOJI = {
    "pending":  "⏳",
    "uploading":"⬆️",
    "encoding": "🔄",
    "creating": "🎨",
    "success":  "✅",
    "failed":   "❌",
}

MAX_WORKERS = 5   # parallel threads per phase

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Config")

    st.subheader("Meta")
    meta_token     = st.text_area("Access Token", height=80, placeholder="Paste Meta access token…")
    meta_token     = meta_token.strip()
    ad_account_id  = st.text_input("Ad Account ID",    value="621093273817881", help="Without act_ prefix")
    page_id        = st.text_input("Facebook Page ID", value="102334935420271")
    campaign_id    = st.text_input("Campaign ID",      value="120254711147130664", help="Reference only")
    adset_id       = st.text_input("Ad Set ID",        value="120254711383200664")
    product_set_id = st.text_input("Product Set ID",   value="1069261742506823")

    st.divider()
    st.subheader("Ad Creative")
    cta_type   = st.selectbox("CTA Type", ["SHOP_NOW", "LEARN_MORE", "SIGN_UP", "DOWNLOAD", "INSTALL_MOBILE_APP"])
    cta_link   = st.text_input("CTA Link", value="http://fkrt.it/cPDqXgNN")
    ad_message = st.text_input("Ad Message", placeholder="Check out our latest!")
    ad_title   = st.text_input("Ad Title",   placeholder="Optional — falls back to filename")

    st.divider()
    st.subheader("Video Source")
    source_mode = st.radio("Source", ["Upload zip file", "Local folder path"], horizontal=True)


# ── Main ──────────────────────────────────────────────────────────────────────
st.title("🎬 MP4 → Meta Video Push")
st.caption("Parallel pipeline: uploads all videos simultaneously, then polls and creates ads in bulk.")

if not meta_token:
    st.info("🔑 Paste your Meta access token in the sidebar.")
    st.stop()

# ── Source ────────────────────────────────────────────────────────────────────
tmp_dir = None

if source_mode == "Upload zip file":
    uploaded_zip = st.file_uploader("Upload your video zip file", type=["zip"])
    if not uploaded_zip:
        st.info("📦 Upload a zip file containing your MP4s.")
        st.stop()
    with st.spinner("Extracting zip…"):
        tmp_dir = extract_zip_to_temp(uploaded_zip.read())
    folder_path = tmp_dir
else:
    folder_path = st.text_input("Local folder path", placeholder="/Users/you/Downloads/drive_videos").strip()
    if not folder_path:
        st.info("📁 Enter the path to your local video folder.")
        st.stop()
    if not os.path.isdir(folder_path):
        st.error(f"Folder not found: `{folder_path}`")
        st.stop()

# ── List files ────────────────────────────────────────────────────────────────
st.subheader("📁 MP4 Files")
try:
    files = list_mp4_files(folder_path)
except Exception as e:
    st.error(f"Could not read files: {e}")
    st.stop()

if not files:
    st.warning("No .mp4 files found.")
    st.stop()

st.success(f"Found **{len(files)}** MP4 file(s)")
with st.expander("Preview files", expanded=True):
    st.dataframe(
        pd.DataFrame([{"Name": f["name"], "Size (MB)": f"{f['size']/1024/1024:.1f}"} for f in files]),
        use_container_width=True, hide_index=True,
    )

# ── Selection ─────────────────────────────────────────────────────────────────
selected_indices = st.multiselect(
    "Select videos to push:",
    options=list(range(len(files))),
    format_func=lambda i: f"{files[i]['name']}  ({files[i]['size']/1024/1024:.1f} MB)",
    default=list(range(len(files))),
)
if not selected_indices:
    st.info("Select at least one video.")
    st.stop()

selected = [files[i] for i in selected_indices]

if not cta_link:
    st.warning("⚠️ Enter a CTA link in the sidebar.")
    st.stop()

st.divider()
st.markdown(
    f"**Campaign:** `{campaign_id}`  |  **Ad Set:** `{adset_id}`  |  "
    f"**Ad Account:** `{ad_account_id}`  |  **Product Set:** `{product_set_id or '—'}`"
)

if not st.button(f"🚀 Push {len(selected)} video(s) to Meta", type="primary"):
    st.stop()

# ── Shared state ──────────────────────────────────────────────────────────────
total = len(selected)
live = [
    {
        "name": f["name"], "status": "pending",
        "video_id": None, "creative_id": None, "ad_id": None, "error": "",
        "file_path": get_local_path(f),
        "ad_name": os.path.splitext(f["name"])[0],
    }
    for f in selected
]

tbl_ph   = st.empty()
phase_ph = st.empty()


def render(ph, rows, phase_label=""):
    if phase_label:
        phase_ph.info(phase_label)
    ph.dataframe(
        pd.DataFrame([{
            "File":     r["name"],
            "Status":   f"{STATUS_EMOJI.get(r['status'], '')} {r['status'].capitalize()}",
            "Video ID": r["video_id"]    or "—",
            "Creative": r["creative_id"] or "—",
            "Ad ID":    r["ad_id"]       or "—",
            "Error":    r["error"]       or "",
        } for r in rows]),
        use_container_width=True, hide_index=True,
    )


render(tbl_ph, live)

# ── Helper: run a phase in parallel ──────────────────────────────────────────
def run_phase(fn_map: dict):
    """
    fn_map: {idx: callable}  — each callable returns its result.
    Runs all in parallel, collects results by idx.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fn): idx for idx, fn in fn_map.items()}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = ("error", str(e))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Upload all videos in parallel
# ═══════════════════════════════════════════════════════════════════════════════
for r in live:
    r["status"] = "uploading"
render(tbl_ph, live, f"📤 Phase 1/4 — Uploading {total} videos in parallel…")

def _upload(idx):
    r = live[idx]
    video_id, err = upload_video(meta_token, ad_account_id, r["file_path"], r["ad_name"])
    return video_id, err

upload_results = run_phase({i: (lambda i=i: _upload(i)) for i in range(total)})

for i, (video_id, err) in upload_results.items():
    if video_id:
        live[i]["video_id"] = video_id
    else:
        live[i].update({"status": "failed", "error": f"Upload: {err}"})

render(tbl_ph, live)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Poll all uploaded videos for ready status in parallel
# ═══════════════════════════════════════════════════════════════════════════════
to_poll = [i for i in range(total) if live[i].get("video_id") and live[i]["status"] != "failed"]
for i in to_poll:
    live[i]["status"] = "encoding"
render(tbl_ph, live, f"⏳ Phase 2/4 — Waiting for {len(to_poll)} videos to finish encoding…")

def _poll(idx):
    return poll_video_ready(meta_token, live[idx]["video_id"])

poll_results = run_phase({i: (lambda i=i: _poll(i)) for i in to_poll})

for i, (ready, msg) in poll_results.items():
    if not ready:
        live[i].update({"status": "failed", "error": f"Encoding: {msg}"})

render(tbl_ph, live)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Create creatives in parallel
# ═══════════════════════════════════════════════════════════════════════════════
to_create = [i for i in to_poll if live[i]["status"] != "failed"]
for i in to_create:
    live[i]["status"] = "creating"
render(tbl_ph, live, f"🎨 Phase 3/4 — Creating {len(to_create)} ad creatives in parallel…")

def _creative(idx):
    r = live[idx]
    name    = r["ad_name"]
    title   = ad_title.strip() or name
    message = ad_message.strip() or name
    ps_id   = product_set_id.strip() or None
    return create_video_creative(
        meta_token, ad_account_id, page_id,
        r["video_id"], name, message, title, cta_type, cta_link,
        product_set_id=ps_id,
    )

creative_results = run_phase({i: (lambda i=i: _creative(i)) for i in to_create})

for i, (creative_id, err) in creative_results.items():
    if creative_id:
        live[i]["creative_id"] = creative_id
    else:
        live[i].update({"status": "failed", "error": f"Creative: {err}"})

render(tbl_ph, live)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — Create ads in parallel
# ═══════════════════════════════════════════════════════════════════════════════
to_ads = [i for i in to_create if live[i].get("creative_id")]
render(tbl_ph, live, f"📢 Phase 4/4 — Creating {len(to_ads)} ads…")

def _ad(idx):
    r = live[idx]
    return create_ad(meta_token, ad_account_id, r["ad_name"], adset_id, r["creative_id"])

ad_results = run_phase({i: (lambda i=i: _ad(i)) for i in to_ads})

for i, (ad_id, err) in ad_results.items():
    if ad_id:
        live[i].update({"status": "success", "ad_id": ad_id})
    else:
        live[i].update({"status": "failed", "error": f"Ad: {err}"})

# Cleanup
if tmp_dir:
    cleanup_temp_dir(tmp_dir)

phase_ph.success("✅ All done!")
render(tbl_ph, live)

# ── Summary ───────────────────────────────────────────────────────────────────
st.divider()
result_df = pd.DataFrame(live)
c1, c2 = st.columns(2)
c1.metric("✅ Success", int((result_df["status"] == "success").sum()))
c2.metric("❌ Failed",  int((result_df["status"] == "failed").sum()))

out_cols  = ["name", "video_id", "creative_id", "ad_id", "status", "error"]
csv_bytes = result_df[[c for c in out_cols if c in result_df.columns]].to_csv(index=False).encode()
st.download_button(
    "⬇️ Download Results CSV", csv_bytes,
    file_name="meta_push_results.csv", mime="text/csv", type="primary",
)
