"""
meta_uploader.py — Meta Graph API helpers
Direct video upload → poll ready → create video creative → create ad.
- All requests have explicit timeouts (no silent hangs)
- Transient failures auto-retry up to 3 times with backoff
"""
import json
import os
import time
import functools

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GRAPH       = "https://graph.facebook.com"
API_VERSION = "v22.0"

# Timeouts: (connect_seconds, read_seconds)
T_DEFAULT = (10, 60)
T_UPLOAD  = (15, 300)   # video upload can be slow on Meta's end
T_POLL    = (10, 30)


# ── Retry decorator ───────────────────────────────────────────────────────────

def _retry(max_attempts=3, wait=3):
    """Retry on requests exceptions (network hiccup, timeout) with backoff."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except (requests.Timeout, requests.ConnectionError) as e:
                    last_err = e
                    if attempt < max_attempts - 1:
                        time.sleep(wait * (attempt + 1))
            raise last_err
        return wrapper
    return decorator


# ── Internal helpers ──────────────────────────────────────────────────────────

def _url(path: str) -> str:
    return f"{GRAPH}/{API_VERSION}/{path}"


@_retry()
def _get(path: str, token: str, timeout=T_DEFAULT, **params) -> dict:
    r = requests.get(
        _url(path),
        params={"access_token": token, **params},
        timeout=timeout,
        verify=False,
    )
    try:
        return r.json()
    except Exception:
        return {"error": {"message": f"{r.status_code}: {r.text[:400]}"}}


@_retry()
def _post(path: str, token: str, data: dict = None, files=None, timeout=T_DEFAULT) -> dict:
    r = requests.post(
        _url(path),
        params={"access_token": token},
        data=data or {},
        files=files,
        timeout=timeout,
        verify=False,
    )
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
    Direct multipart upload. Returns (video_id, error).
    Uses a generous 5-minute read timeout — Meta can be slow to acknowledge.
    """
    with open(file_path, "rb") as f:
        d = _post(
            f"act_{ad_account_id}/advideos",
            token,
            data={"name": name},
            files={"source": (os.path.basename(file_path), f, "video/mp4")},
            timeout=T_UPLOAD,
        )
    if err := _err(d):
        return None, err
    video_id = d.get("id")
    if not video_id:
        return None, f"no video_id in response: {d}"
    return video_id, None


def poll_video_ready(
    token: str, video_id: str, max_retries: int = 60, interval: int = 5
) -> tuple[bool, str]:
    """
    Poll until status = ready. ~5 min max.
    Returns (is_ready, message).

    Meta returns status in two possible shapes:
      {"status": {"video_status": "ready"}}   ← advideos node
      {"status": "READY"}                      ← some API versions return a plain string
    Also surfaces API errors so empty-status doesn't silently loop.
    """
    vs = ""
    last_raw = ""
    for attempt in range(max_retries):
        d = _get(video_id, token, timeout=T_POLL, fields="status,id")

        # Surface any API error immediately — no point retrying a permission error
        if api_err := _err(d):
            return False, f"poll API error: {api_err}"

        raw_status = d.get("status", "")

        # Shape 1: {"status": {"video_status": "ready"}}
        if isinstance(raw_status, dict):
            vs = raw_status.get("video_status", "")
        # Shape 2: {"status": "READY"} or {"status": "processing"}
        elif isinstance(raw_status, str):
            vs = raw_status.lower()

        last_raw = repr(raw_status)

        if vs in ("ready", "ready_to_publish"):
            return True, "ready"
        if vs == "error":
            return False, f"processing error: {raw_status}"

        time.sleep(interval)

    return False, f"timed out after {max_retries * interval}s — last response status={last_raw}. Check that your token has ads_management permission and the video_id {video_id!r} is in your ad account."


def get_video_thumbnail(token: str, video_id: str) -> str | None:
    """Fetch Meta's auto-generated thumbnail URI for the video."""
    d = _get(video_id, token, timeout=T_POLL, fields="thumbnails")
    thumbs = d.get("thumbnails", {}).get("data", [])
    if not thumbs:
        return None
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
    """Create a video ad creative. Returns (creative_id, error)."""
    video_data: dict = {
        "video_id": video_id,
        "message":  message,
        "title":    title,
        "call_to_action": {
            "type":  cta_type,
            "value": {"link": cta_link},
        },
    }
    thumb_url = get_video_thumbnail(token, video_id)
    if thumb_url:
        video_data["image_url"] = thumb_url

    story_spec = {"page_id": page_id, "video_data": video_data}
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
