# Copyright @r790
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import aiohttp
import socket
import time
import re
import base64
import json
import gzip
import brotli
import zstandard as zstd
import cloudscraper
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import threading
import uuid
import os

app = FastAPI(title="Smart TempMail API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TempMailService:
    def __init__(self):
        self.sessions = {}
        self.email_sessions = {}

    async def decode_api_url(self, encoded_url: str) -> Optional[str]:
        try:
            cleaned_url = re.sub(r'[^A-Za-z0-9+/=]', '', encoded_url)
            cleaned_url = cleaned_url.replace('f56', '6')
            cleaned_url = cleaned_url + '=' * (4 - len(cleaned_url) % 4) if len(cleaned_url) % 4 != 0 else cleaned_url
            decoded = base64.b64decode(cleaned_url).decode('utf-8')
            if not decoded.startswith('http'):
                decoded = 'https://' + decoded.lstrip('?:/')
            return decoded
        except Exception as e:
            print(f"[DEBUG] Error decoding API URL: {str(e)}")
            return None

    async def decompress_response(self, response_text: str, headers: dict) -> str:
        if headers.get('Content-Encoding') == 'gzip':
            try:
                return gzip.decompress(response_text.encode()).decode('utf-8')
            except Exception as e:
                print(f"[DEBUG] Error decompressing response: {str(e)}")
                return response_text
        return response_text

    def decompress_edu_response(self, response):
        content = response.content
        try:
            if not content:
                return None
            if response.headers.get('content-encoding') == 'gzip':
                return gzip.decompress(content).decode('utf-8')
            elif response.headers.get('content-encoding') == 'br':
                try:
                    return brotli.decompress(content).decode('utf-8')
                except brotli.error:
                    return content.decode('utf-8', errors='ignore')
            elif response.headers.get('content-encoding') == 'zstd':
                try:
                    dctx = zstd.ZstdDecompressor()
                    return dctx.decompress(content).decode('utf-8')
                except zstd.ZstdError:
                    return content.decode('utf-8', errors='ignore')
            return content.decode('utf-8')
        except Exception:
            return None

    async def extract_auth_token(self, html_content: str, cookies: dict) -> Optional[str]:
        try:
            jwt_patterns = [
                r'"jwt"\s*:\s*"(eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.[A-Za-z0-9_-]+)"',
                r'"token"\s*:\s*"(eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.[A-Za-z0-9_-]+)"'
            ]
            for pattern in jwt_patterns:
                matches = re.findall(pattern, html_content, re.IGNORECASE)
                for match in matches:
                    if isinstance(match, str) and match.startswith('eyJ'):
                        return match
            return None
        except Exception as e:
            print(f"[DEBUG] Error extracting auth token: {str(e)}")
            return None

    async def extract_email_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        try:
            email_input = soup.find('input', {'id': 'mail'})
            if email_input and email_input.get('value'):
                return email_input.get('value')
            email_span = soup.find('span', {'id': 'mail'})
            if email_span and email_span.get_text(strip=True):
                return email_span.get_text(strip=True)
            return None
        except Exception as e:
            print(f"[DEBUG] Error extracting email from HTML: {str(e)}")
            return None

    async def get_mailbox_and_token(self, api_url: str, cookies: dict, scraper, ten_minute: bool = False) -> tuple:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://temp-mail.org',
                'Referer': 'https://temp-mail.org/',
            }
            response = scraper.post(f"{api_url}/mailbox", headers=headers, cookies=cookies, json={})
            if response.status_code == 200:
                data = response.json()
                email = data.get('mailbox')
                token = data.get('token')
                return email, token
            return None, None
        except Exception as e:
            print(f"[DEBUG] Exception in get_mailbox_and_token: {str(e)}")
            return None, None

    async def check_inbox(self, api_url: str, auth_token: str, cookies: dict, scraper) -> Optional[list]:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Authorization': f'Bearer {auth_token}',
                'Referer': 'https://temp-mail.org/',
            }
            response = scraper.get(f"{api_url}/messages", headers=headers, cookies=cookies)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"[DEBUG] Exception in check_inbox: {str(e)}")
            return None

    async def generate_temp_mail(self, ten_minute: bool = False) -> Dict[str, Any]:
        scraper = cloudscraper.create_scraper()
        try:
            url = 'https://temp-mail.org/en/10minutemail' if ten_minute else 'https://temp-mail.org/en/'
            response = scraper.get(url)
            if response.status_code != 200:
                raise HTTPException(status_code=503, detail="Could not connect to the email provider.")
            
            html_content = response.text
            cookies = dict(response.cookies)
            soup = BeautifulSoup(html_content, 'html.parser')
            
            api_url = "https://web2.temp-mail.org" # Hardcode for reliability
            
            email, auth_token = await self.get_mailbox_and_token(api_url, cookies, scraper, ten_minute)

            if not email:
                email = await self.extract_email_from_html(soup)
            if not auth_token:
                auth_token = await self.extract_auth_token(html_content, cookies)

            if not email or not auth_token:
                raise HTTPException(status_code=503, detail="The email provider is currently unavailable or blocking requests. Please try the EDU option.")

            session_data = {'api_url': api_url, 'email': email, 'cookies': cookies, 'scraper': scraper, 'created_at': time.time(), 'ten_minute': ten_minute}
            self.sessions[auth_token] = session_data
            
            return {
                "temp_mail": email,
                "access_token": auth_token,
                "expires_at": (datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S') if ten_minute else "N/A"
            }
        except Exception as e:
            print(f"[ERROR] generate_temp_mail: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    async def check_messages(self, token: str) -> Dict[str, Any]:
        if token not in self.sessions:
            raise HTTPException(status_code=404, detail="Invalid or expired token")
        
        session = self.sessions[token]
        if session['ten_minute'] and (time.time() - session['created_at']) > 600:
            del self.sessions[token]
            raise HTTPException(status_code=410, detail="10-minute email has expired")

        try:
            messages_raw = await self.check_inbox(session['api_url'], token, session['cookies'], session['scraper'])
            if messages_raw is None:
                raise HTTPException(status_code=500, detail="Failed to check inbox")

            # **FIX**: Standardize the message format
            standardized_messages = []
            for msg in messages_raw:
                sender = (msg.get('fromAddress', {}).get('address')) or 'Unknown Sender'
                received_at = msg.get('receivedAt')
                date_str = datetime.fromtimestamp(received_at).strftime('%Y-%m-%d %H:%M:%S') if received_at else 'N/A'
                
                standardized_messages.append({
                    "from": sender,
                    "subject": msg.get('subject', 'No Subject'),
                    "date": date_str,
                    "body": msg.get('text', 'No content available.')
                })

            return {"mailbox": session['email'], "messages": standardized_messages}
        except Exception as e:
            print(f"[ERROR] check_messages: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error checking messages: {str(e)}")

    async def get_edu_email(self):
        url = "https://etempmail.com/getEmailAddress"
        headers = {'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'}
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.post(url, headers=headers)
            if response.status_code == 200:
                data = json.loads(self.decompress_edu_response(response))
                return data['address'], data['recover_key'], response.cookies.get_dict()
        except Exception:
            return None, None, None
        return None, None, None

    async def check_edu_inbox(self, cookies):
        url = "https://etempmail.com/getInbox"
        headers = {'origin': 'https://etempmail.com', 'referer': 'https://etempmail.com/'}
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.post(url, headers=headers, cookies=cookies)
            if response.status_code == 200:
                return json.loads(self.decompress_edu_response(response))
        except Exception:
            return []
        return []

    async def generate_edu_email(self):
        try:
            email, recover_key, cookies = await self.get_edu_email()
            if not email:
                raise HTTPException(status_code=500, detail="Failed to generate EDU email")
            access_token = str(uuid.uuid4())
            self.email_sessions[access_token] = {"email": email, "cookies": cookies, "created_at": time.time()}
            return {"edu_mail": email, "access_token": access_token}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def check_edu_messages(self, token: str):
        if token not in self.email_sessions:
            raise HTTPException(status_code=404, detail="Invalid or expired EDU token")
        session = self.email_sessions[token]
        try:
            inbox_raw = await self.check_edu_inbox(session["cookies"])
            
            # **FIX**: Standardize the message format with lowercase keys
            standardized_messages = []
            for mail in inbox_raw:
                soup = BeautifulSoup(mail.get('body', ''), 'html.parser')
                body_text = soup.get_text(strip=True)
                standardized_messages.append({
                    "from": mail.get('from', 'Unknown Sender'),
                    "subject": mail.get('subject', 'No Subject'),
                    "date": mail.get('date', 'N/A'),
                    "body": body_text or 'No content available.'
                })
            
            return {"edu_mail": session["email"], "messages": standardized_messages}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

temp_mail_service = TempMailService()

# --- API Endpoints ---
@app.get("/")
async def root():
    return JSONResponse(content={"message": "TempMail API is running."})

@app.get("/api/gen")
async def generate_mail():
    return await temp_mail_service.generate_temp_mail(ten_minute=False)

@app.get("/api/chk")
async def check_mail(token: str):
    return await temp_mail_service.check_messages(token)

@app.get("/api/10min/gen")
async def generate_10min_mail():
    return await temp_mail_service.generate_temp_mail(ten_minute=True)

@app.get("/api/10min/chk")
async def check_10min_mail(token: str):
    return await temp_mail_service.check_messages(token)

@app.get("/api/edu/gen")
async def generate_edu_email_endpoint():
    return await temp_mail_service.generate_edu_email()

@app.get("/api/edu/chk")
async def check_edu_messages_endpoint(token: str):
    return await temp_mail_service.check_edu_messages(token)

# --- Server Startup ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

