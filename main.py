# main.py — EDU-only Temp Mail API (Render-ready)

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
import cloudscraper
import json
import time
import uuid
import threading
import socket
import os
from datetime import datetime

# ----- App bootstrap -----
app = FastAPI(
    title="Smart EDU TempMail API",
    version="1.0.0",
    description="EDU-only temporary mailbox API."
)

# CORS (adjust allow_origins if you want to restrict)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["https://your-frontend.example"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- Service -----
class EduMailService:
    """
    EDU-only mailbox service backed by etempmail.com endpoints.
    This is a trimmed version of your existing implementation, keeping only EDU.
    """
    def __init__(self):
        # token -> session
        self.email_sessions: Dict[str, Dict[str, Any]] = {}

    # ---- Remote calls (etempmail) ----
    def _scraper(self):
        # Dedicated creator (Cloudflare-friendly)
        return cloudscraper.create_scraper()

    def _common_headers(self) -> Dict[str, str]:
        return {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
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
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/140.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }

    def _decompress_body(self, resp) -> Optional[str]:
        """
        Safely decode body for gzip/br/plain. (zstd not expected here)
        """
        try:
            content = resp.content or b""
            if not content:
                return None
            enc = (resp.headers.get("content-encoding") or "").lower()
            if enc == "gzip":
                import gzip
                return gzip.decompress(content).decode("utf-8", errors="ignore")
            if enc == "br":
                import brotli
                try:
                    return brotli.decompress(content).decode("utf-8", errors="ignore")
                except brotli.error:
                    return content.decode("utf-8", errors="ignore")
            return content.decode("utf-8", errors="ignore")
        except Exception:
            return None

    def _get_edu_email(self):
        """
        Calls etempmail to allocate an address + recover key + cookies.
        """
        url = "https://etempmail.com/getEmailAddress"
        s = self._scraper()
        headers = self._common_headers()

        for attempt in range(3):
            try:
                r = s.post(url, headers=headers)
                if r.status_code == 200:
                    text = self._decompress_body(r)
                    if not text:
                        time.sleep(1.5)
                        continue
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

    def _get_edu_inbox(self, email: str, cookies: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Returns array of raw messages (as provided by etempmail).
        """
        url = "https://etempmail.com/getInbox"
        s = self._scraper()
        headers = self._common_headers()

        try:
            r = s.post(url, headers=headers, cookies=cookies)
            if r.status_code == 200:
                text = self._decompress_body(r)
                if not text:
                    return []
                try:
                    data = json.loads(text)
                    # etempmail returns a list of mails
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
            return []
        except Exception:
            return []

    # ---- Public helpers used by routes ----
    def generate_edu_email(self) -> Dict[str, Any]:
        email, recover_key, cookies = self._get_edu_email()
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
            "api_owner": "@ISmartCoder",
            "api_dev": "@TheSmartDev",
            "edu_mail": email,
            "access_token": token,
        }

    def check_edu_messages(self, token: str) -> Dict[str, Any]:
        session = self.email_sessions.get(token)
        if not session:
            raise HTTPException(status_code=404, detail="Invalid or expired token")

        email = session["email"]
        cookies = session["cookies"]
        raw = self._get_edu_inbox(email, cookies)

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
            "api_owner": "@ISmartCoder",
            "api_dev": "@TheSmartDev",
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


edu_service = EduMailService()

# ----- Routes (EDU-only) -----
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}

@app.get("/api/edu/gen")
def api_edu_gen():
    return JSONResponse(content=edu_service.generate_edu_email())

@app.get("/api/edu/chk")
def api_edu_chk(token: str):
    return JSONResponse(content=edu_service.check_edu_messages(token))

# Optional: explicitly block any legacy routes if someone calls them
from fastapi import APIRouter
disabled = APIRouter()

def _disabled(detail: str = "Disabled: EDU-only API"):
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

@disabled.get("/api/gen")            # regular temp mail (disabled)
@disabled.get("/api/chk")
@disabled.get("/api/10min/gen")      # 10-minute mail (disabled)
@disabled.get("/api/10min/chk")
def _legacy_block():
    _disabled()

app.include_router(disabled)

# ----- Session cleanup (housekeeping) -----
def _cleanup_expired_sessions():
    # 2h TTL for EDU mail sessions
    TTL = 2 * 60 * 60
    while True:
        now = time.time()
        expired = [t for t, s in edu_service.email_sessions.items()
                   if now - s.get("created_at", now) > TTL]
        for t in expired:
            edu_service.email_sessions.pop(t, None)
        time.sleep(300)

def _get_local_ip():
    try:
        import socket as _sock
        with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

# Start cleanup thread when running via `python main.py`
if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=_cleanup_expired_sessions, daemon=True).start()
    ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    print("EDU TempMail API starting…")
    print(f"Health:        http://{ip}:{port}/healthz")
    print(f"EDU Generate:  http://{ip}:{port}/api/edu/gen")
    print(f"EDU Check:     http://{ip}:{port}/api/edu/chk?token=YOUR_TOKEN")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
