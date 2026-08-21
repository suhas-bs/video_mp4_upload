"""
app.py — MP4 → Meta Video Push Tool
Processes one video at a time end-to-end:
  upload → poll encoding → create creative → create ad → next video
"""

import os
import time

import pandas as pd
import streamlit as st

from drive_helper import (
    cleanup_temp_dir,
    extract_zip_to_temp,
    get_local_path,
    list_mp4_files,
)
from meta_uploader import (
    check_video_status,
    create_ad,
    create_video_creative,
    upload_video,
)

st.set_page_config(page_title="MP4 → Meta Video Push", page_icon="🎬", layout="wide")

STATUS_EMOJI = {
    "pending":   "⏳",
    "uploading": "⬆️",
    "encoding":  "🔄",
    "creating":  "🎨",
    "success":   "✅",
    "failed":    "❌",
}

POLL_INTERVAL   = 15   # seconds between each encoding check
POLL_MAX_SECS   = 600  # 10 minutes max per video
RATE_LIMIT_WAIT = 60   # back off when rate limited

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
st.caption("Processes one video at a time: upload → encode → creative → ad → next.")

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
    folder_path = st.text_input(
        "Local folder path", placeholder="/Users/you/Downloads/drive_videos"
    ).strip()
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

# ── State ─────────────────────────────────────────────────────────────────────
total = len(selected)
live = [
    {
        "name":        f["name"],
        "status":      "pending",
        "video_id":    None,
        "creative_id": None,
        "ad_id":       None,
        "error":       "",
        "file_path":   get_local_path(f),
        "ad_name":     os.path.splitext(f["name"])[0],
    }
    for f in selected
]

tbl_ph   = st.empty()
phase_ph = st.empty()


def render(label=""):
    if label:
        phase_ph.info(label)
    tbl_ph.dataframe(
        pd.DataFrame([{
            "File":     r["name"],
            "Status":   f"{STATUS_EMOJI.get(r['status'], '')} {r['status'].capitalize()}",
            "Video ID": r["video_id"]    or "—",
            "Creative": r["creative_id"] or "—",
            "Ad ID":    r["ad_id"]       or "—",
            "Error":    r["error"]       or "",
        } for r in live]),
        use_container_width=True, hide_index=True,
    )


render()

# ── Process each video sequentially ──────────────────────────────────────────
for idx, row in enumerate(live):
    label_prefix = f"[{idx+1}/{total}] {row['name']}"

    # Step 1: Upload
    row["status"] = "uploading"
    render(f"⬆️ {label_prefix} — uploading…")

    video_id, err = upload_video(
        meta_token, ad_account_id, row["file_path"], row["ad_name"]
    )
    if not video_id:
        row.update({"status": "failed", "error": f"Upload: {err}"})
        render()
        continue

    row["video_id"] = video_id
    row["status"]   = "encoding"
    render(f"🔄 {label_prefix} — waiting for Meta to finish encoding…")

    # Step 2: Poll encoding — one check at a time, no hidden retries
    deadline = time.time() + POLL_MAX_SECS
    ready    = False

    while time.time() < deadline:
        vs, poll_err = check_video_status(meta_token, video_id)

        if vs == "rate_limited":
            render(f"⚠️ {label_prefix} — rate limited, backing off {RATE_LIMIT_WAIT}s…")
            time.sleep(RATE_LIMIT_WAIT)
            continue

        if vs == "ready":
            ready = True
            break

        if vs == "error":
            row.update({"status": "failed", "error": f"Encoding error: {poll_err}"})
            render()
            break

        # "processing", "network_error", or anything else → wait and retry
        elapsed = int(time.time() - (deadline - POLL_MAX_SECS))
        render(f"🔄 {label_prefix} — encoding ({vs}) — {elapsed}s elapsed, checking again in {POLL_INTERVAL}s…")
        time.sleep(POLL_INTERVAL)

    if not ready:
        if row["status"] != "failed":
            row.update({"status": "failed", "error": f"Encoding timed out after {POLL_MAX_SECS//60} min"})
        render()
        continue

    # Step 3: Create creative
    row["status"] = "creating"
    render(f"🎨 {label_prefix} — creating ad creative…")

    creative_id, err = create_video_creative(
        meta_token, ad_account_id, page_id,
        video_id,
        row["ad_name"],
        ad_message.strip() or row["ad_name"],
        ad_title.strip()   or row["ad_name"],
        cta_type, cta_link,
        product_set_id=product_set_id.strip() or None,
    )
    if not creative_id:
        row.update({"status": "failed", "error": f"Creative: {err}"})
        render()
        continue

    row["creative_id"] = creative_id

    # Step 4: Create ad
    render(f"📢 {label_prefix} — creating ad…")

    ad_id, err = create_ad(meta_token, ad_account_id, row["ad_name"], adset_id, creative_id)
    if not ad_id:
        row.update({"status": "failed", "error": f"Ad: {err}"})
        render()
        continue

    row.update({"status": "success", "ad_id": ad_id})
    render(f"✅ {label_prefix} — done!")

# ── Cleanup & summary ─────────────────────────────────────────────────────────
if tmp_dir:
    cleanup_temp_dir(tmp_dir)

phase_ph.success("✅ All videos processed!")
render()

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
