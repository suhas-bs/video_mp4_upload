"""
app.py — MP4 → Meta Video Push Tool
-------------------------------------
Flow:
  1. Upload a zip OR point to a local folder  →  list MP4s
  2. Select videos  →  configure creative details
  3. Push           →  live row-by-row status table
  4. Download results CSV
"""

import os

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
    "pending": "⏳",
    "running": "🔄",
    "success": "✅",
    "failed":  "❌",
}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Config")

    st.subheader("Meta")
    meta_token    = st.text_area("Access Token", height=80, placeholder="Paste Meta access token…")
    meta_token    = meta_token.strip()
    ad_account_id = st.text_input("Ad Account ID",    value="1549883851784009", help="Without act_ prefix")
    page_id       = st.text_input("Facebook Page ID", value="336701269535125")
    campaign_id   = st.text_input("Campaign ID",      value="120254711147130664", help="Reference only — ad is placed under Ad Set ID")
    adset_id      = st.text_input("Ad Set ID",        value="120254711147130664")
    product_set_id = st.text_input("Product Set ID",  value="1069261742506823", help="Catalogue product set linked to all creatives")

    st.divider()
    st.subheader("Ad Creative")
    cta_type   = st.selectbox("CTA Type", ["SHOP_NOW", "LEARN_MORE", "SIGN_UP", "DOWNLOAD", "INSTALL_MOBILE_APP"])
    cta_link   = st.text_input("CTA Link", placeholder="https://...")
    ad_message = st.text_input("Ad Message", placeholder="Check out our latest!")
    ad_title   = st.text_input("Ad Title",   placeholder="Optional — falls back to filename")

    st.divider()
    st.subheader("Video Source")
    source_mode = st.radio("Source", ["Upload zip file", "Local folder path"], horizontal=True)


# ── Main ──────────────────────────────────────────────────────────────────────
st.title("🎬 MP4 → Meta Video Push")
st.caption("Reads MP4s from a zip or local folder → uploads to Meta → creates video ads under your ad set.")

if not meta_token:
    st.info("🔑 Paste your Meta access token in the sidebar.")
    st.stop()

# ── Source: zip upload or folder ──────────────────────────────────────────────
tmp_dir  = None
files    = []

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

# ── List MP4s ─────────────────────────────────────────────────────────────────
st.subheader("📁 MP4 Files")

try:
    files = list_mp4_files(folder_path)
except Exception as e:
    st.error(f"Could not read files: {e}")
    st.stop()

if not files:
    st.warning("No .mp4 files found.")
    st.stop()

file_df = pd.DataFrame([{
    "Name":      f["name"],
    "Size (MB)": f"{f['size'] / 1024 / 1024:.1f}",
} for f in files])

st.success(f"Found **{len(files)}** MP4 file(s)")
with st.expander("Preview files", expanded=True):
    st.dataframe(file_df, use_container_width=True, hide_index=True)

# ── Selection ─────────────────────────────────────────────────────────────────
selected_indices = st.multiselect(
    "Select videos to push to Meta:",
    options=list(range(len(files))),
    format_func=lambda i: f"{files[i]['name']}  ({files[i]['size'] / 1024 / 1024:.1f} MB)",
    default=list(range(len(files))),
)

if not selected_indices:
    st.info("Select at least one video.")
    st.stop()

selected = [files[i] for i in selected_indices]

if not cta_link:
    st.warning("⚠️ Enter a CTA link in the sidebar before pushing.")
    st.stop()

st.divider()
st.markdown(
    f"**Campaign:** `{campaign_id}`  |  **Ad Set:** `{adset_id}`  |  "
    f"**Ad Account:** `{ad_account_id}`  |  **Product Set:** `{product_set_id or '—'}`"
)

if not st.button(f"🚀 Push {len(selected)} video(s) to Meta", type="primary"):
    st.stop()

# ── Live status table ─────────────────────────────────────────────────────────
live = [
    {"name": f["name"], "status": "pending",
     "video_id": None, "creative_id": None, "ad_id": None, "error": ""}
    for f in selected
]
tbl_ph   = st.empty()
prog_bar = st.progress(0)
total    = len(selected)


def render(ph, rows):
    ph.dataframe(
        pd.DataFrame([{
            "File":     r["name"],
            "Status":   f"{STATUS_EMOJI.get(r['status'], '')} {r['status'].capitalize()}",
            "Video ID": r["video_id"]    or "—",
            "Creative": r["creative_id"] or "—",
            "Ad ID":    r["ad_id"]       or "—",
            "Error":    r["error"]       or "",
        } for r in rows]),
        use_container_width=True,
        hide_index=True,
    )


render(tbl_ph, live)
results = []

try:
    for idx, (finfo, row) in enumerate(zip(selected, live)):
        fname     = finfo["name"]
        file_path = get_local_path(finfo)
        name      = os.path.splitext(fname)[0]
        title     = ad_title.strip() or name
        message   = ad_message.strip() or name
        ps_id     = product_set_id.strip() or None

        prog_bar.progress(idx / total, text=f"Processing {fname} ({idx + 1}/{total})…")
        row["status"] = "running"
        render(tbl_ph, live)

        try:
            # Step 1 — Upload to Meta (direct multipart)
            with st.spinner(f"⬆️ Uploading {fname}…"):
                video_id, err = upload_video(meta_token, ad_account_id, file_path, name)

            if not video_id:
                row.update({"status": "failed", "error": f"Upload: {err}"})
                render(tbl_ph, live)
                results.append({**finfo, **row})
                continue

            row["video_id"] = video_id
            render(tbl_ph, live)

            # Step 2 — Wait for encoding
            with st.spinner(f"⏳ Encoding {fname}…"):
                ready, msg = poll_video_ready(meta_token, video_id)

            if not ready:
                row.update({"status": "failed", "error": f"Not ready: {msg}"})
                render(tbl_ph, live)
                results.append({**finfo, **row})
                continue

            # Step 3 — Create creative
            creative_id, err = create_video_creative(
                meta_token, ad_account_id, page_id,
                video_id, name, message, title, cta_type, cta_link,
                product_set_id=ps_id,
            )

            if not creative_id:
                row.update({"status": "failed", "error": f"Creative: {err}"})
                render(tbl_ph, live)
                results.append({**finfo, **row})
                continue

            row["creative_id"] = creative_id

            # Step 4 — Create ad
            ad_id, err = create_ad(meta_token, ad_account_id, name, adset_id, creative_id)

            if ad_id:
                row.update({"status": "success", "ad_id": ad_id})
            else:
                row.update({"status": "failed", "error": f"Ad: {err}"})

        except Exception as exc:
            row.update({"status": "failed", "error": str(exc)})

        render(tbl_ph, live)
        results.append({**finfo, **row})

finally:
    # Clean up temp extraction dir if we used zip upload
    if tmp_dir:
        cleanup_temp_dir(tmp_dir)

prog_bar.progress(1.0, text="Done!")

# ── Summary ───────────────────────────────────────────────────────────────────
st.divider()
result_df = pd.DataFrame(results)

c1, c2 = st.columns(2)
c1.metric("✅ Success", int((result_df["status"] == "success").sum()))
c2.metric("❌ Failed",  int((result_df["status"] == "failed").sum()))

out_cols  = ["name", "video_id", "creative_id", "ad_id", "status", "error"]
csv_bytes = result_df[[c for c in out_cols if c in result_df.columns]].to_csv(index=False).encode()
st.download_button(
    "⬇️ Download Results CSV", csv_bytes,
    file_name="meta_push_results.csv", mime="text/csv", type="primary",
)
