# Copyright @r790
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import time
import json
import gzip
import brotli
import zstandard as zstd
import cloudscraper
from bs4 import BeautifulSoup
import uuid
import os

# --- FastAPI APP ---
app = FastAPI(title="EDU Mail API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TempMailService:
    def __init__(self):
        self.email_sessions = {}

    def decompress_edu_response(self, response):
        """Safely decompress the response from the email provider."""
        content = response.content
        try:
            if not content: return None
            encoding = response.headers.get('content-encoding')
            if encoding == 'gzip': return gzip.decompress(content).decode('utf-8')
            if encoding == 'br': return brotli.decompress(content).decode('utf-8')
            if encoding == 'zstd': return zstd.decompress(content).decode('utf-8')
            return content.decode('utf-8')
        except Exception as e:
            print(f"[ERROR] Decompression failed: {e}")
            return None

    async def get_edu_email(self):
        """Fetches an EDU email directly without a proxy."""
        scraper = cloudscraper.create_scraper()
        try:
            print("Attempting to get EDU email directly...")
            response = scraper.post(
                "https://etempmail.com/getEmailAddress",
                headers={'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'}
            )
            
            if response.status_code == 200:
                decompressed_data = self.decompress_edu_response(response)
                if decompressed_data:
                    data = json.loads(decompressed_data)
                    if 'address' in data and 'recover_key' in data:
                        print("Successfully fetched EDU email.")
                        return data['address'], data['recover_key'], response.cookies.get_dict()
            
            print(f"[WARN] Failed to fetch EDU email. Status code: {response.status_code}")
            return None, None, None
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred while fetching EDU email: {e}")
            return None, None, None

    async def check_edu_inbox(self, cookies):
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.post(
                "https://etempmail.com/getInbox",
                headers={'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'},
                cookies=cookies
            )
            if response.status_code == 200:
                return json.loads(self.decompress_edu_response(response))
        except Exception as e:
            print(f"[ERROR] Failed to check EDU inbox: {e}")
        return []

    async def generate_edu_email(self):
        email, _, cookies = await self.get_edu_email()
        if not email:
            raise HTTPException(status_code=503, detail="The email provider is currently unavailable or is blocking requests from this server.")
        
        access_token = str(uuid.uuid4())
        self.email_sessions[access_token] = {"email": email, "cookies": cookies}
        return {"edu_mail": email, "access_token": access_token}

    async def check_edu_messages(self, token: str):
        if token not in self.email_sessions:
            raise HTTPException(status_code=404, detail="Invalid EDU token")
        session = self.email_sessions[token]
        try:
            inbox_raw = await self.check_edu_inbox(session["cookies"])
            standardized_messages = []
            for mail in inbox_raw:
                standardized_messages.append({
                    "from": mail.get('from', 'Unknown'),
                    "subject": mail.get('subject', 'No Subject'),
                    "date": mail.get('date', 'N/A'),
                    "body": BeautifulSoup(mail.get('body', ''), 'html.parser').get_text(strip=True)
                })
            return {"edu_mail": session["email"], "messages": standardized_messages}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

temp_mail_service = TempMailService()

@app.get("/")
async def root(): return {"message": "EDU Mail API is running."}

@app.get("/api/edu/gen")
async def generate_edu_email_endpoint(): return await temp_mail_service.generate_edu_email()

@app.get("/api/edu/chk")
async def check_edu_messages_endpoint(token: str): return await temp_mail_service.check_edu_messages(token)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

