# Copyright @r790
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import time
import json
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
            if encoding == 'gzip':
                import gzip
                return gzip.decompress(content).decode('utf-8')
            if encoding == 'br':
                import brotli
                return brotli.decompress(content).decode('utf-8')
            if encoding == 'zstd':
                import zstandard as zstd
                return zstd.decompress(content).decode('utf-8')
            return content.decode('utf-8')
        except Exception as e:
            print(f"[ERROR] Decompression failed: {e}")
            return None

    async def get_edu_email_with_retries(self, max_retries=3):
        """Attempt to fetch an email, retrying on failure."""
        scraper = cloudscraper.create_scraper()
        for attempt in range(max_retries):
            try:
                print(f"Attempt {attempt + 1} to get EDU email...")
                response = scraper.post("https://etempmail.com/getEmailAddress", headers={'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'})
                
                if response.status_code != 200:
                    print(f"[WARN] Received status code {response.status_code} on attempt {attempt + 1}")
                    time.sleep(1)
                    continue

                decompressed_data = self.decompress_edu_response(response)
                if not decompressed_data:
                    print(f"[WARN] Failed to decompress response on attempt {attempt + 1}")
                    time.sleep(1)
                    continue

                data = json.loads(decompressed_data)
                
                if 'address' in data and 'recover_key' in data:
                    print("Successfully fetched EDU email.")
                    return data['address'], data['recover_key'], response.cookies.get_dict()
                else:
                    print(f"[WARN] Response JSON missing required keys on attempt {attempt + 1}")
                    time.sleep(1)

            except json.JSONDecodeError as e:
                print(f"[ERROR] JSON Decode Error on attempt {attempt + 1}: {e}")
                print(f"Raw response: {response.text[:200]}")
                time.sleep(1)
            except Exception as e:
                print(f"[ERROR] An unexpected error occurred on attempt {attempt + 1}: {e}")
                time.sleep(1)
        
        print("All retries to get EDU email failed.")
        return None, None, None

    async def check_edu_inbox(self, cookies):
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.post("https://etempmail.com/getInbox", headers={'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'}, cookies=cookies)
            if response.status_code == 200:
                return json.loads(self.decompress_edu_response(response))
        except Exception as e:
            print(f"[ERROR] Failed to check EDU inbox: {e}")
        return []

    async def generate_edu_email(self):
        email, _, cookies = await self.get_edu_email_with_retries()
        if not email:
            raise HTTPException(status_code=503, detail="The external email provider is currently unavailable. Please try again in a few moments.")
        
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

