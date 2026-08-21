"""
meta_uploader.py — Meta Graph API helpers
Upload → poll encoding → create creative → create ad.
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


def _url(path: str) -> str:
    return f"{GRAPH}/{API_VERSION}/{path}"


def _err(d: dict) -> str | None:
    if "error" not in d:
        return None
    e = d["error"]
    msg = e.get("message", str(d))
    for k in ("error_user_msg", "error_user_title"):
        if e.get(k):
            msg += f" | {e[k]}"
    return msg


def _retry(max_attempts=3, wait=3):
    """Retry on network errors with backoff. Only for upload/create — NOT for polling."""
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


@_retry()
def _post(path: str, token: str, data: dict = None, files=None,
          timeout=(15, 300)) -> dict:
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


def _get_once(path: str, token: str, timeout=(10, 20), **params) -> dict:
    """Single GET with no retry — used for polling so we never silently hang."""
    try:
        r = requests.get(
            _url(path),
            params={"access_token": token, **params},
            timeout=timeout,
            verify=False,
        )
        return r.json()
    except Exception as e:
        # Return a transient marker — caller treats this as "try again"
        return {"_network_error": str(e)}


@_retry()
def _get(path: str, token: str, timeout=(10, 30), **params) -> dict:
    """Retrying GET — used for creative/thumbnail fetches, not polling."""
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


# ── Public API ────────────────────────────────────────────────────────────────

def upload_video(
    token: str, ad_account_id: str, file_path: str, name: str
) -> tuple[str | None, str | None]:
    """Direct multipart upload. Returns (video_id, error)."""
    with open(file_path, "rb") as f:
        d = _post(
            f"act_{ad_account_id}/advideos",
            token,
            data={"name": name},
            files={"source": (os.path.basename(file_path), f, "video/mp4")},
        )
    if err := _err(d):
        return None, err
    video_id = d.get("id")
    if not video_id:
        return None, f"no video_id in response: {d}"
    return video_id, None


def check_video_status(token: str, video_id: str) -> tuple[str, str | None]:
    """
    Single non-blocking status check.
    Returns (status, error_or_None).
    status: "ready" | "processing" | "rate_limited" | "error" | "network_error"
    Callers should treat "network_error" the same as "processing" (try again).
    """
    d = _get_once(video_id, token, fields="status,id")

    # Network hiccup — just try again next interval
    if "_network_error" in d:
        return "network_error", d["_network_error"]

    # Rate limit
    err_code = d.get("error", {}).get("code")
    if err_code == 4:
        return "rate_limited", None

    # Other API error
    if api_err := _err(d):
        return "error", f"poll API error: {api_err}"

    raw = d.get("status", "")
    if isinstance(raw, dict):
        vs = raw.get("video_status", "processing").lower()
    elif isinstance(raw, str):
        vs = raw.strip().lower()
    else:
        vs = "processing"

    if vs in ("ready", "ready_to_publish"):
        return "ready", None
    if vs == "error":
        return "error", f"Meta encoding error: {raw}"
    return vs or "processing", None


def get_video_thumbnail(token: str, video_id: str) -> str | None:
    """Fetch Meta's preferred thumbnail URI."""
    d = _get(video_id, token, fields="thumbnails")
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

    data: dict = {
        "name":              name,
        "object_story_spec": json.dumps({"page_id": page_id, "video_data": video_data}),
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
