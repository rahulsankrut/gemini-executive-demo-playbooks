#!/usr/bin/env python3
"""Cloud Run & Local HTTP server for the Executive Storybook Rack.

Features:
- Optional server-side password gate (configurable via STORYBOOK_PASSWORD env var or local .env)
- Signed HttpOnly session cookie + X-Access-Password header support for CLI feedback reporting
- Durable Google Cloud Storage persistence (GCS_BUCKET env var) with optimistic generation locking,
  falling back seamlessly to local feedback_state.json during local development
- Attributed inline edit tracking (Before -> After diffs) + page-anchored comment threads
"""

import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def _load_dotenv_file(dotenv_path: Path):
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_file(ROOT / ".env")

PORT = int(os.environ.get("PORT", "8847"))
LOCAL_STATE_FILE = ROOT / "feedback_state.json"
LEGACY_EDITS_FILE = ROOT / "edits.json"

ACCESS_PASSWORD = os.environ.get("STORYBOOK_PASSWORD", "").strip()
COOKIE_SECRET = os.environ.get(
    "STORYBOOK_COOKIE_SECRET",
    hashlib.sha256(f"agy-executive-storybook::{ACCESS_PASSWORD}".encode("utf-8")).hexdigest(),
)
COOKIE_NAME = "agy_sb_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET", "").strip()
GCS_OBJECT_NAME = os.environ.get("GCS_STATE_OBJECT", "storybook_feedback_state.json").strip()
GATE_KICKER = os.environ.get("STORYBOOK_GATE_KICKER", "🔒 EXECUTIVE WORKSHOP · TEAM REVIEW GATE").strip()

ALLOWED_ASSETS = {
    "Digital_Chief_of_Staff_Agent.docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "Alphabet_Q3_2026_Form_10-Q.pdf": "application/pdf",
}

DEFAULT_ROSTER = [
    "Executive Sponsor",
    "Product Lead",
    "Engineering Lead",
    "Finance Lead",
    "Legal Lead",
]
_raw_roster = os.environ.get("STORYBOOK_ROSTER", "").strip()
TEAM_ROSTER = (
    [item.strip() for item in _raw_roster.split(";") if item.strip()]
    if _raw_roster
    else DEFAULT_ROSTER
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_auth_token() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(COOKIE_SECRET.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_auth_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    ts_str, sig = token.split(".", 1)
    if not ts_str.isdigit():
        return False
    expected = hmac.new(COOKIE_SECRET.encode("utf-8"), ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    age = int(time.time()) - int(ts_str)
    return -300 <= age <= COOKIE_MAX_AGE


def default_state() -> dict:
    edits = {}
    if LEGACY_EDITS_FILE.exists():
        try:
            loaded = json.loads(LEGACY_EDITS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                edits = loaded
        except Exception:
            pass
    return {
        "edits": edits,
        "edit_log": [],
        "comments": [],
    }


def normalize_ws(val: str) -> str:
    return " ".join(str(val or "").split())


def normalize_state(data: dict) -> dict:
    if not isinstance(data, dict):
        return default_state()
    if not isinstance(data.get("edits"), dict):
        data["edits"] = {}
    if not isinstance(data.get("edit_log"), list):
        data["edit_log"] = []
    if not isinstance(data.get("comments"), list):
        data["comments"] = []

    edits_map = data["edits"]
    cleaned_log = []
    latest_after_by_key = {}
    for entry in data["edit_log"]:
        if not isinstance(entry, dict):
            continue
        ekey = str(entry.get("editKey") or "").strip()
        b_txt = normalize_ws(entry.get("beforeText", ""))
        a_txt = normalize_ws(entry.get("afterText", ""))
        # Strip phantom DOM visibility entries where element was never in edits_map and one side was blank
        if ekey not in edits_map and (not b_txt or not a_txt):
            continue
        if b_txt == a_txt:
            continue
        latest_after_by_key[ekey] = a_txt
        cleaned_log.append(entry)

    # Mark each log entry with whether that element is STILL actively changed right now
    for entry in cleaned_log:
        ekey = str(entry.get("editKey") or "").strip()
        orig_txt = normalize_ws(entry.get("originalText") or entry.get("beforeText") or "")
        cur_txt = latest_after_by_key.get(ekey, "")
        entry["currentlyChanged"] = bool(ekey in edits_map and cur_txt != orig_txt)

    data["edit_log"] = cleaned_log
    return data


class StateStore:
    """Thread-safe + GCS generation-safe collaborative state store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._gcs_bucket = None
        if GCS_BUCKET_NAME:
            from google.cloud import storage  # Installed in Cloud Run image

            client = storage.Client()
            self._gcs_bucket = client.bucket(GCS_BUCKET_NAME)

    def read(self) -> dict:
        if self._gcs_bucket is not None:
            blob = self._gcs_bucket.blob(GCS_OBJECT_NAME)
            if not blob.exists():
                return default_state()
            raw = blob.download_as_text(encoding="utf-8")
            return normalize_state(json.loads(raw))

        with self._lock:
            if not LOCAL_STATE_FILE.exists():
                return default_state()
            try:
                return normalize_state(json.loads(LOCAL_STATE_FILE.read_text(encoding="utf-8")))
            except Exception:
                return default_state()

    def mutate(self, mutator_fn) -> dict:
        if self._gcs_bucket is not None:
            from google.api_core.exceptions import PreconditionFailed

            for _ in range(6):
                blob = self._gcs_bucket.blob(GCS_OBJECT_NAME)
                if blob.exists():
                    blob.reload()
                    gen = blob.generation
                    state = normalize_state(json.loads(blob.download_as_text(encoding="utf-8")))
                else:
                    gen = 0
                    state = default_state()

                updated = normalize_state(mutator_fn(state))
                payload = json.dumps(updated, indent=2, ensure_ascii=False)
                try:
                    blob.upload_from_string(
                        payload,
                        content_type="application/json; charset=utf-8",
                        if_generation_match=gen,
                    )
                    return updated
                except PreconditionFailed:
                    time.sleep(0.08)
            # Final fallback write if extreme contention
            blob = self._gcs_bucket.blob(GCS_OBJECT_NAME)
            state = self.read()
            updated = normalize_state(mutator_fn(state))
            blob.upload_from_string(
                json.dumps(updated, indent=2, ensure_ascii=False),
                content_type="application/json; charset=utf-8",
            )
            return updated

        with self._lock:
            state = default_state()
            if LOCAL_STATE_FILE.exists():
                try:
                    state = normalize_state(json.loads(LOCAL_STATE_FILE.read_text(encoding="utf-8")))
                except Exception:
                    state = default_state()
            updated = normalize_state(mutator_fn(state))
            LOCAL_STATE_FILE.write_text(json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
            return updated


STORE = StateStore()


def parse_reviewer(raw_rev) -> dict:
    if isinstance(raw_rev, dict):
        name = str(raw_rev.get("name") or "").strip()
        email = str(raw_rev.get("email") or "").strip()
        full = str(raw_rev.get("full") or "").strip()
        if not full:
            full = f"{name} <{email}>" if (name and email) else (name or "Anonymous Reviewer")
        if not name:
            name = full.split("<")[0].strip() or "Anonymous Reviewer"
        return {"name": name, "email": email, "full": full}
    if isinstance(raw_rev, str) and raw_rev.strip():
        s = raw_rev.strip()
        if "<" in s and s.endswith(">"):
            n, em = s.split("<", 1)
            return {"name": n.strip(), "email": em[:-1].strip(), "full": s}
        return {"name": s, "email": "", "full": s}
    return {"name": "Anonymous Reviewer", "email": "", "full": "Anonymous Reviewer"}


class StorybookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send_json(self, status: int, data: dict, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, filepath: Path, status: int = 200):
        content = filepath.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _is_authenticated(self) -> bool:
        if not ACCESS_PASSWORD:
            return True
        header_pw = self.headers.get("X-Access-Password", "")
        if header_pw and hmac.compare_digest(header_pw, ACCESS_PASSWORD):
            return True
        cookie_hdr = self.headers.get("Cookie", "")
        if cookie_hdr:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(cookie_hdr)
                if COOKIE_NAME in jar and verify_auth_token(jar[COOKIE_NAME].value):
                    return True
            except Exception:
                pass
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if route == "/api/public-config":
            self._send_json(200, {
                "ok": True,
                "gate_kicker": GATE_KICKER,
                "roster": TEAM_ROSTER,
            })
            return

        if route in ("/", "/index.html"):
            if not self._is_authenticated():
                self._send_html_file(ROOT / "gate.html", 200)
                return
            self._send_html_file(ROOT / "index.html", 200)
            return

        if route == "/feedback":
            if not self._is_authenticated():
                self.send_response(302)
                self.send_header("Location", "/?next=/feedback")
                self.end_headers()
                return
            self._send_html_file(ROOT / "feedback.html", 200)
            return

        if route.startswith("/assets/"):
            if not self._is_authenticated():
                self.send_response(302)
                self.send_header("Location", f"/?next={route}")
                self.end_headers()
                return
            asset_name = route[len("/assets/"):]
            content_type = ALLOWED_ASSETS.get(asset_name)
            asset_path = (ROOT / "assets" / asset_name).resolve()
            if (
                not content_type
                or asset_path.parent != (ROOT / "assets").resolve()
                or not asset_path.is_file()
            ):
                self._send_json(404, {"ok": False, "error": "Asset not found"})
                return
            data = asset_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{asset_name}"')
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if route in ("/api/edits", "/api/state"):
            if not self._is_authenticated():
                self._send_json(401, {"ok": False, "error": "Authentication required"})
                return
            state = STORE.read()
            if route == "/api/edits":
                self._send_json(200, state["edits"])
            else:
                self._send_json(200, {
                    "ok": True,
                    "edits": state["edits"],
                    "comments": state["comments"],
                    "edit_log": state["edit_log"],
                    "roster": TEAM_ROSTER,
                })
            return

        if route == "/api/feedback":
            if not self._is_authenticated():
                self._send_json(401, {"ok": False, "error": "Authentication required"})
                return
            state = STORE.read()
            open_comments = sum(1 for c in state["comments"] if not c.get("resolved"))
            changed_edits = [e for e in state["edit_log"] if e.get("currentlyChanged", True)]
            authors = sorted({
                *(c.get("authorFull", "") for c in state["comments"] if c.get("authorFull")),
                *(e.get("authorFull", "") for e in changed_edits if e.get("authorFull")),
            })
            self._send_json(200, {
                "ok": True,
                "summary": {
                    "total_comments": len(state["comments"]),
                    "open_comments": open_comments,
                    "changed_edits": len(changed_edits),
                    "total_edits": len(changed_edits),
                    "all_historical_edits": len(state["edit_log"]),
                    "active_reviewers": authors,
                },
                "comments": state["comments"],
                "edit_log": state["edit_log"],
                "edits": state["edits"],
            })
            return

        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/login":
            try:
                body = self._read_json_body()
            except Exception:
                self._send_json(400, {"ok": False, "error": "Invalid JSON"})
                return
            pw = str(body.get("password") or "")
            if ACCESS_PASSWORD and not hmac.compare_digest(pw, ACCESS_PASSWORD):
                self._send_json(401, {"ok": False, "error": "Incorrect password. Please check with the workshop host and try again."})
                return
            token = make_auth_token()
            is_https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            secure_attr = "; Secure" if is_https else ""
            cookie_val = (
                f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax{secure_attr}"
            )
            self._send_json(200, {"ok": True}, extra_headers=[("Set-Cookie", cookie_val)])
            return

        if route == "/api/logout":
            cookie_val = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            self._send_json(200, {"ok": True}, extra_headers=[("Set-Cookie", cookie_val)])
            return

        if not self._is_authenticated():
            self._send_json(401, {"ok": False, "error": "Authentication required"})
            return

        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        if route == "/api/save":
            edits = payload.get("edits", {})
            if not isinstance(edits, dict):
                edits = {}
            changes = payload.get("changes", [])
            if not isinstance(changes, list):
                changes = []
            rev = parse_reviewer(payload.get("reviewer"))
            now = utc_now_iso()

            def _apply_save(state: dict) -> dict:
                # Merge live edits map so concurrent editors on different keys preserve each other's changes
                merged_edits = dict(state.get("edits", {}))
                for k, v in edits.items():
                    merged_edits[k] = v
                for rk in payload.get("revertedKeys", []) or []:
                    merged_edits.pop(str(rk), None)
                # Also remove keys if explicitly reset
                if payload.get("replaceAll") is True:
                    merged_edits = edits
                state["edits"] = merged_edits

                log = list(state.get("edit_log", []))
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    ekey = str(ch.get("editKey") or "").strip()
                    orig_txt = normalize_ws(ch.get("originalText") or ch.get("beforeText") or "")
                    before_txt = normalize_ws(ch.get("beforeText") or "")
                    after_txt = normalize_ws(ch.get("afterText") or "")
                    if not ekey or before_txt == after_txt:
                        continue

                    # Coalesce rapid keystroke saves by same author on same editKey
                    merged_existing = False
                    for idx in range(len(log) - 1, -1, -1):
                        prev = log[idx]
                        if prev.get("editKey") == ekey and prev.get("authorFull") == rev["full"]:
                            prev["afterText"] = after_txt
                            prev["updatedAt"] = now
                            if normalize_ws(prev.get("beforeText")) == after_txt:
                                log.pop(idx)
                            merged_existing = True
                            break
                        if prev.get("editKey") == ekey:
                            # Different reviewer touched this element more recently; start fresh entry
                            break

                    if not merged_existing:
                        log.append({
                            "id": f"ed-{uuid.uuid4().hex[:10]}",
                            "type": "edit",
                            "authorName": rev["name"],
                            "authorEmail": rev["email"],
                            "authorFull": rev["full"],
                            "bookIdx": ch.get("bookIdx", -1),
                            "bookTitle": str(ch.get("bookTitle") or ""),
                            "slideIdx": ch.get("slideIdx", 0),
                            "slideTitle": str(ch.get("slideTitle") or ""),
                            "editKey": ekey,
                            "originalText": orig_txt,
                            "beforeText": before_txt,
                            "afterText": after_txt,
                            "updatedAt": now,
                        })
                state["edit_log"] = log
                return state

            updated = STORE.mutate(_apply_save)
            self._send_json(200, {
                "ok": True,
                "count": len(updated["edits"]),
                "edit_log_count": len(updated["edit_log"]),
            })
            return

        if route == "/api/comment":
            text = str(payload.get("text") or "").strip()
            if not text:
                self._send_json(400, {"ok": False, "error": "Comment text is required"})
                return
            rev = parse_reviewer(payload.get("reviewer"))
            new_cmt = {
                "id": f"cmt-{uuid.uuid4().hex[:10]}",
                "type": "comment",
                "authorName": rev["name"],
                "authorEmail": rev["email"],
                "authorFull": rev["full"],
                "bookIdx": payload.get("bookIdx", -1),
                "bookTitle": str(payload.get("bookTitle") or ""),
                "slideIdx": payload.get("slideIdx", 0),
                "slideTitle": str(payload.get("slideTitle") or ""),
                "text": text,
                "resolved": False,
                "resolvedBy": None,
                "replies": [],
                "createdAt": utc_now_iso(),
            }

            def _add_comment(state: dict) -> dict:
                state["comments"].append(new_cmt)
                return state

            updated = STORE.mutate(_add_comment)
            self._send_json(200, {"ok": True, "comment": new_cmt, "comments": updated["comments"]})
            return

        if route == "/api/comment/reply":
            cid = str(payload.get("commentId") or "").strip()
            text = str(payload.get("text") or "").strip()
            if not cid or not text:
                self._send_json(400, {"ok": False, "error": "commentId and text are required"})
                return
            rev = parse_reviewer(payload.get("reviewer"))
            reply_obj = {
                "id": f"rep-{uuid.uuid4().hex[:8]}",
                "authorName": rev["name"],
                "authorEmail": rev["email"],
                "authorFull": rev["full"],
                "text": text,
                "createdAt": utc_now_iso(),
            }

            def _add_reply(state: dict) -> dict:
                for c in state["comments"]:
                    if c.get("id") == cid:
                        c.setdefault("replies", []).append(reply_obj)
                        break
                return state

            updated = STORE.mutate(_add_reply)
            self._send_json(200, {"ok": True, "comments": updated["comments"]})
            return

        if route == "/api/comment/resolve":
            cid = str(payload.get("commentId") or "").strip()
            resolved = bool(payload.get("resolved", True))
            rev = parse_reviewer(payload.get("reviewer"))

            def _resolve_cmt(state: dict) -> dict:
                for c in state["comments"]:
                    if c.get("id") == cid:
                        c["resolved"] = resolved
                        c["resolvedBy"] = rev["full"] if resolved else None
                        break
                return state

            updated = STORE.mutate(_resolve_cmt)
            self._send_json(200, {"ok": True, "comments": updated["comments"]})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})


if __name__ == "__main__":
    os.chdir(ROOT)
    with http.server.ThreadingHTTPServer(("", PORT), StorybookHandler) as httpd:
        mode = f"GCS ({GCS_BUCKET_NAME}/{GCS_OBJECT_NAME})" if GCS_BUCKET_NAME else f"Local ({LOCAL_STATE_FILE.name})"
        print(f"Serving Gated Collaborative Storybook on http://0.0.0.0:{PORT} [Storage: {mode}]")
        httpd.serve_forever()
