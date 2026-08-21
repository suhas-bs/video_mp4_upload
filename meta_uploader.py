"""
meta_uploader.py — Meta Graph API helpers
Resumable video upload → poll ready → create video creative → create ad.

No BCA/partnership ad code needed — pure MP4 upload flow.
"""
import json
import os
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GRAPH = "https://graph.facebook.com"
API_VERSION = "v22.0"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _url(path: str) -> str:
    return f"{GRAPH}/{API_VERSION}/{path}"


def _get(path: str, token: str, **params) -> dict:
    r = requests.get(_url(path), params={"access_token": token, **params}, verify=False)
    try:
        return r.json()
    except Exception:
        return {"error": {"message": f"{r.status_code}: {r.text[:400]}"}}


def _post(path: str, token: str, data: dict = None, files=None) -> dict:
    params = {"access_token": token}
    r = requests.post(_url(path), params=params, data=data or {}, files=files, verify=False)
    try:
        return r.json()
    except Exception:
        return {"error": {"message": f"{r.status_code}: {r.text[:400]}"}}


def _err(d: dict) -> str | None:
    if "error" not in d:
        return None
    e = d["error"]
    msg = e.get("message", str(d))
    for k in ("error_user_msg", "error_user_title"):
        if e.get(k):
            msg += f" | {e[k]}"
    return msg


# ── Public API ────────────────────────────────────────────────────────────────

def upload_video(
    token: str, ad_account_id: str, file_path: str, name: str
) -> tuple[str | None, str | None]:
    """
    Direct multipart upload to act_{ad_account_id}/advideos.
    Returns (video_id, error_message).
    Works for files up to ~1 GB. Simpler than chunked upload and
    returns the video id immediately without a session dance.
    """
    endpoint = f"act_{ad_account_id}/advideos"
    with open(file_path, "rb") as f:
        d = _post(
            endpoint, token,
            data={"name": name},
            files={"source": (os.path.basename(file_path), f, "video/mp4")},
        )
    if err := _err(d):
        return None, err
    video_id = d.get("id")
    if not video_id:
        return None, f"no video id in response: {d}"
    return video_id, None


def poll_video_ready(
    token: str, video_id: str, max_retries: int = 36, interval: int = 5
) -> tuple[bool, str]:
    """
    Poll video status until 'ready'. ~3 min max by default.
    Returns (is_ready, message).
    """
    for _ in range(max_retries):
        d = _get(video_id, token, fields="status")
        vs = d.get("status", {}).get("video_status", "")
        if vs == "ready":
            return True, "ready"
        if vs == "error":
            return False, f"processing error: {d.get('status')}"
        time.sleep(interval)
    return False, f"timed out after {max_retries * interval}s (last status: {vs!r})"


def get_video_thumbnail(token: str, video_id: str) -> str | None:
    """
    Fetch the auto-generated thumbnail URI from a processed video.
    Returns the preferred thumbnail URL, or None if unavailable.
    """
    d = _get(video_id, token, fields="thumbnails")
    thumbs = d.get("thumbnails", {}).get("data", [])
    if not thumbs:
        return None
    # prefer the one Meta marks as preferred, else take first
    preferred = next((t for t in thumbs if t.get("is_preferred")), thumbs[0])
    return preferred.get("uri")


def create_video_creative(
    token: str,
    ad_account_id: str,
    page_id: str,
    video_id: str,
    name: str,
    message: str,
    title: str,
    cta_type: str,
    cta_link: str,
    product_set_id: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Create a video ad creative. Returns (creative_id, error).
    Optionally attaches a product set (catalogue) to the creative.
    """
    video_data: dict = {
        "video_id": video_id,
        "message":  message,
        "title":    title,
        "call_to_action": {
            "type":  cta_type,
            "value": {"link": cta_link},
        },
    }
    # Meta requires a thumbnail — fetch the auto-generated one
    thumb_url = get_video_thumbnail(token, video_id)
    if thumb_url:
        video_data["image_url"] = thumb_url

    story_spec = {
        "page_id":    page_id,
        "video_data": video_data,
    }
    data = {
        "name":              name,
        "object_story_spec": json.dumps(story_spec),
    }
    if product_set_id:
        data["degrees_of_freedom_spec"] = json.dumps({
            "creative_features_spec": {
                "product_extensions": {"enroll_status": "OPT_IN"}
            }
        })
        data["creative_sourcing_spec"] = json.dumps({
            "associated_product_set_id": str(product_set_id)
        })

    d = _post(f"act_{ad_account_id}/adcreatives", token, data=data)
    if err := _err(d):
        return None, err
    return d.get("id"), None


def create_ad(
    token: str,
    ad_account_id: str,
    ad_name: str,
    ad_set_id: str,
    creative_id: str,
) -> tuple[str | None, str | None]:
    """Create ad in PAUSED status. Returns (ad_id, error)."""
    d = _post(
        f"act_{ad_account_id}/ads", token,
        data={
            "name":     ad_name,
            "adset_id": ad_set_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status":   "PAUSED",
        },
    )
    if err := _err(d):
        return None, err
    return d.get("id"), None
