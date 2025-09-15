# main.py — EDU-only API with STATELESS inbox (r790)
# © r790

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import cloudscraper

import os, time, json, uuid, threading, socket
from datetime import datetime
from typing import Dict, Any, List, Optional

# compression libs for upstream responses
import gzip, brotli
import zstandard as zstd

# stateless session helpers
import hmac, hashlib, base64

APP_TITLE = "r790 EDU TempMail API"
APP_VERSION = "2.0.0"

# ──────────────────────────────────────────────────────────────────────────────
# App bootstrap
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title=APP_TITLE, version=APP_VERSION, description="EDU-only temporary mailbox API by r790.")

# Permissive CORS so a Netlify frontend can call the API directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten to your Netlify domain if you wish
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Secret for signing the stateless session blob (set in Render env if possible)
SECRET = os.getenv("EDU_BLOB_SECRET", "please-change-me")


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────
def _scraper():
    return cloudscraper.create_scraper()

def _common_headers() -> Dict[str, str]:
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.6",
        "origin": "https://etempmail.com",
        "referer": "https://etempmail.com/",
        "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Brave";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-gpc": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }

def _decompress_body(resp) -> Optional[str]:
    try:
        content = resp.content or b""
        if not content:
            return None
        enc = (resp.headers.get("content-encoding") or "").lower()
        if enc == "gzip":
            return gzip.decompress(content).decode("utf-8", errors="ignore")
        if enc == "br":
            try:
                return brotli.decompress(content).decode("utf-8", errors="ignore")
            except brotli.error:
                return content.decode("utf-8", errors="ignore")
        if enc == "zstd":
            try:
                dctx = zstd.ZstdDecompressor()
                return dctx.decompress(content).decode("utf-8", errors="ignore")
            except zstd.ZstdError:
                return content.decode("utf-8", errors="ignore")
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return None

# ── stateless blob helpers
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64url_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()

def make_session_blob(email: str, cookies: Dict[str, str], recover_key: str) -> str:
    raw = json.dumps({"e": email, "c": cookies, "rk": recover_key}, separators=(",", ":")).encode()
    return f"{_b64url(raw)}.{_sign(raw)}"

def read_session_blob(blob: str) -> Dict[str, Any]:
    try:
        b64, sig = blob.split(".", 1)
        raw = _b64url_dec(b64)
        if not hmac.compare_digest(_sign(raw), sig):
            raise ValueError("bad signature")
        return json.loads(raw.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid blob")


# ──────────────────────────────────────────────────────────────────────────────
# EDU service (etempmail.com)
# ──────────────────────────────────────────────────────────────────────────────
class EduService:
    def __init__(self):
        # legacy, in-memory sessions for /api/edu/gen + /api/edu/chk
        self.email_sessions: Dict[str, Dict[str, Any]] = {}

    # Low-level remote calls
    def get_edu_email(self):
        url = "https://etempmail.com/getEmailAddress"
        s = _scraper()
        headers = _common_headers()
        for _ in range(3):
            try:
                r = s.post(url, headers=headers)
                if r.status_code == 200:
                    text = _decompress_body(r)
                    if not text:
                        time.sleep(1.2); continue
                    try:
                        data = json.loads(text)
                        address = data.get("address")
                        recover_key = data.get("recover_key")
                        cookies = r.cookies.get_dict()
                        if address and recover_key:
                            return address, recover_key, cookies
                    except json.JSONDecodeError:
                        pass
                time.sleep(1.0)
            except Exception:
                time.sleep(1.0)
        return None, None, None

    def get_edu_inbox(self, email: str, cookies: Dict[str, str]) -> List[Dict[str, Any]]:
        url = "https://etempmail.com/getInbox"
        s = _scraper()
        headers = _common_headers()
        try:
            r = s.post(url, headers=headers, cookies=cookies)
            if r.status_code == 200:
                text = _decompress_body(r)
                if not text:
                    return []
                try:
                    data = json.loads(text)
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
            return []
        except Exception:
            return []

    # Public (legacy)
    def generate_edu_email(self) -> Dict[str, Any]:
        email, recover_key, cookies = self.get_edu_email()
        if not email:
            raise HTTPException(status_code=500, detail="Failed to generate EDU email")
        token = str(uuid.uuid4())
        self.email_sessions[token] = {
            "email": email,
            "recover_key": recover_key,
            "cookies": cookies,
            "created_at": time.time(),
        }
        return {
            "copyright": "r790",
            "edu_mail": email,
            "access_token": token,
        }

    def check_edu_messages(self, token: str) -> Dict[str, Any]:
        session = self.email_sessions.get(token)
        if not session:
            # this is the 404 users see when instances rotate on Render
            raise HTTPException(status_code=404, detail="Invalid or expired token")
        email = session["email"]
        cookies = session["cookies"]
        raw = self.get_edu_inbox(email, cookies)
        messages: List[Dict[str, Any]] = []
        for mail in raw:
            body_html = mail.get("body", "")
            soup = BeautifulSoup(body_html, "html.parser")
            body_text = soup.get_text().strip()
            messages.append({
                "From": mail.get("from", ""),
                "Subject": mail.get("subject", ""),
                "Date": mail.get("date", ""),
                "body": body_text,
                "Message": body_text,
            })
        resp: Dict[str, Any] = {
            "copyright": "r790",
            "edu_mail": email,
            "access_token": token,
            "messages": messages,
        }
        if messages:
            latest = messages[0]
            resp.update({
                "Message": latest["Message"],
                "From": latest["From"],
                "body": latest["body"],
                "Date": latest["Date"],
                "Subject": latest["Subject"],
            })
        else:
            resp.update({"Message": "", "From": "", "body": "", "Date": "", "Subject": ""})
        return resp

    # Public (STATELESS)
    def generate_edu_email_stateless(self) -> Dict[str, Any]:
        email, recover_key, cookies = self.get_edu_email()
        if not email:
            raise HTTPException(status_code=500, detail="Failed to generate EDU email")
        blob = make_session_blob(email, cookies, recover_key or "")
        return {
            "copyright": "r790",
            "edu_mail": email,
            "session_blob": blob,
        }

    def check_edu_messages_stateless(self, blob: str) -> Dict[str, Any]:
        data = read_session_blob(blob)   # {'e':..., 'c':..., 'rk':...}
        email = data["e"]; cookies = data["c"]
        raw = self.get_edu_inbox(email, cookies)
        messages: List[Dict[str, Any]] = []
        for mail in raw:
            soup = BeautifulSoup(mail.get("body", ""), "html.parser")
            body_text = soup.get_text().strip()
            messages.append({
                "From": mail.get("from", ""),
                "Subject": mail.get("subject", ""),
                "Date": mail.get("date", ""),
                "body": body_text,
                "Message": body_text,
            })
        resp: Dict[str, Any] = {
            "copyright": "r790",
            "edu_mail": email,
            "messages": messages,
        }
        if messages:
            latest = messages[0]
            resp.update({
                "Message": latest["Message"],
                "From": latest["From"],
                "body": latest["body"],
                "Date": latest["Date"],
                "Subject": latest["Subject"],
            })
        else:
            resp.update({"Message": "", "From": "", "body": "", "Date": "", "Subject": ""})
        return resp


edu = EduService()


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z", "copyright": "r790"}

# Serve index.html if you keep it in this same service (optional)
@app.get("/")
def index():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"copyright": "r790", "message": "EDU API online"})

# Legacy (stateful) endpoints — keep if your current frontend still calls them
@app.get("/api/edu/gen")
def api_edu_gen():
    return JSONResponse(content=edu.generate_edu_email())

@app.get("/api/edu/chk")
def api_edu_chk(token: str):
    return JSONResponse(content=edu.check_edu_messages(token))

# New STATELESS endpoints — recommended for Netlify + Render
@app.get("/api/edu/gen2")
def api_edu_gen2():
    return JSONResponse(content=edu.generate_edu_email_stateless())

@app.get("/api/edu/chk2")
def api_edu_chk2(blob: str):
    return JSONResponse(content=edu.check_edu_messages_stateless(blob))


# ──────────────────────────────────────────────────────────────────────────────
# Housekeeping
# ──────────────────────────────────────────────────────────────────────────────
def _cleanup_expired_sessions():
    # Purge legacy in-memory tokens after 2h
    TTL = 2 * 60 * 60
    while True:
        now = time.time()
        expired = [t for t, s in edu.email_sessions.items() if now - s.get("created_at", now) > TTL]
        for t in expired:
            edu.email_sessions.pop(t, None)
        time.sleep(300)

def _get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=_cleanup_expired_sessions, daemon=True).start()
    ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    print(f"{APP_TITLE} starting…")
    print(f"Health:        http://{ip}:{port}/healthz")
    print(f"EDU gen:       http://{ip}:{port}/api/edu/gen   (stateful)")
    print(f"EDU chk:       http://{ip}:{port}/api/edu/chk?token=...   (stateful)")
    print(f"EDU gen2:      http://{ip}:{port}/api/edu/gen2  (STATELESS)")
    print(f"EDU chk2:      http://{ip}:{port}/api/edu/chk2?blob=...   (STATELESS)")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
