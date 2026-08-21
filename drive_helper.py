"""
drive_helper.py — Local MP4 scanner + zip extractor
No Drive API needed. Point the app at a local folder or upload a zip.
"""
import io
import os
import tempfile
import zipfile


def list_mp4_files(folder_path: str) -> list[dict]:
    """Return all .mp4 files (recursively) as list of {id (full path), name, size}."""
    files = []
    for root, _, fnames in os.walk(folder_path):
        for fname in sorted(fnames):
            if fname.lower().endswith(".mp4") and not fname.startswith(".__"):
                full_path = os.path.join(root, fname)
                files.append({
                    "id":   full_path,
                    "name": fname,
                    "size": os.path.getsize(full_path),
                })
    return sorted(files, key=lambda f: f["name"])


def get_local_path(file_info: dict) -> str:
    return file_info["id"]


def extract_zip_to_temp(zip_bytes: bytes) -> str:
    """Extract a zip to a new temp directory. Returns the temp dir path."""
    tmp_dir = tempfile.mkdtemp(prefix="meta_push_")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(tmp_dir)
    return tmp_dir


def cleanup_temp_dir(tmp_dir: str):
    """Remove temp extraction directory."""
    import shutil
    if tmp_dir and os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
