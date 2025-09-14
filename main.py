# Copyright @r790
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import requests
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
import random

# --- DYNAMIC PROXY CONFIGURATION ---
PROXY_API_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
PROXY_LIST = []
LAST_PROXY_FETCH = 0
PROXY_FETCH_COOLDOWN = 600 # 10 minutes

def update_proxy_list():
    """Fetches a new list of proxies from the API."""
    global PROXY_LIST, LAST_PROXY_FETCH
    current_time = time.time()
    # Only fetch if the list is empty or older than the cooldown period
    if not PROXY_LIST or (current_time - LAST_PROXY_FETCH > PROXY_FETCH_COOLDOWN):
        try:
            print("Fetching new proxy list...")
            response = requests.get(PROXY_API_URL, timeout=10)
            response.raise_for_status() # Raise an exception for bad status codes
            
            # The API returns proxies separated by newlines
            proxies = response.text.strip().split('\n')
            # Filter out any empty lines
            PROXY_LIST = [p.strip() for p in proxies if p.strip()]
            LAST_PROXY_FETCH = current_time
            print(f"Successfully fetched {len(PROXY_LIST)} proxies.")
        except requests.RequestException as e:
            print(f"Error fetching proxy list: {e}")
            # Clear the list if fetching fails to avoid using a stale list
            PROXY_LIST = []

def get_random_proxy():
    """Returns a random proxy from the list, updating the list if needed."""
    update_proxy_list()
    if PROXY_LIST:
        proxy = random.choice(PROXY_LIST)
        return {
            "http": proxy,
            "https": proxy
        }
    return None

# --- FastAPI APP ---
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

    def decompress_edu_response(self, response):
        content = response.content
        try:
            if not content: return None
            encoding = response.headers.get('content-encoding')
            if encoding == 'gzip': return gzip.decompress(content).decode('utf-8')
            if encoding == 'br': return brotli.decompress(content).decode('utf-8')
            if encoding == 'zstd': return zstd.decompress(content).decode('utf-8')
            return content.decode('utf-8')
        except Exception:
            return None

    async def extract_auth_token(self, html_content: str) -> Optional[str]:
        try:
            match = re.search(r'"token"\s*:\s*"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"', html_content)
            if match:
                return match.group(1)
        except Exception:
            return None
        return None

    async def extract_email_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        try:
            email_input = soup.find('input', {'id': 'mail'})
            if email_input:
                return email_input.get('value')
        except Exception:
            return None
        return None

    async def get_mailbox_and_token(self, api_url: str, cookies: dict, scraper, proxies: dict) -> tuple:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            response = scraper.post(f"{api_url}/mailbox", headers=headers, cookies=cookies, json={}, proxies=proxies)
            if response.status_code == 200:
                data = response.json()
                return data.get('mailbox'), data.get('token')
        except Exception as e:
            print(f"Error getting mailbox: {e}")
        return None, None

    async def check_inbox(self, api_url: str, auth_token: str, cookies: dict, scraper, proxies: dict) -> Optional[list]:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Authorization': f'Bearer {auth_token}'
            }
            response = scraper.get(f"{api_url}/messages", headers=headers, cookies=cookies, proxies=proxies)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"Error checking inbox: {e}")
        return None

    async def generate_temp_mail(self, ten_minute: bool = False) -> Dict[str, Any]:
        max_retries = 5
        for attempt in range(max_retries):
            proxies = get_random_proxy()
            if not proxies:
                print("No working proxies available. Attempting direct connection.")
            
            scraper = cloudscraper.create_scraper()
            try:
                print(f"Attempt {attempt + 1}/{max_retries} using proxy: {proxies.get('http') if proxies else 'None'}")
                url = 'https://temp-mail.org/en/10minutemail' if ten_minute else 'https://temp-mail.org/en/'
                response = scraper.get(url, proxies=proxies, timeout=15)
                
                if response.status_code == 200:
                    html_content = response.text
                    cookies = dict(response.cookies)
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    api_url = "https://web2.temp-mail.org"
                    email, auth_token = await self.get_mailbox_and_token(api_url, cookies, scraper, proxies)

                    if not email: email = await self.extract_email_from_html(soup)
                    if not auth_token: auth_token = await self.extract_auth_token(html_content)

                    if email and auth_token:
                        session_data = {'api_url': api_url, 'email': email, 'cookies': cookies, 'scraper': scraper, 'created_at': time.time(), 'ten_minute': ten_minute}
                        self.sessions[auth_token] = session_data
                        return {"temp_mail": email, "access_token": auth_token}
                else:
                    print(f"Attempt {attempt + 1} failed with status code: {response.status_code}")

            except Exception as e:
                print(f"Attempt {attempt + 1} failed with error: {e}")
                continue # Try next proxy

        raise HTTPException(status_code=503, detail="All proxy attempts failed. The service may be temporarily unavailable.")

    async def check_messages(self, token: str) -> Dict[str, Any]:
        if token not in self.sessions:
            raise HTTPException(status_code=404, detail="Invalid or expired token")
        
        session = self.sessions[token]
        proxies = get_random_proxy() # Use a proxy for checking too, just in case.
        try:
            messages_raw = await self.check_inbox(session['api_url'], token, session['cookies'], session['scraper'], proxies)
            if messages_raw is None:
                raise HTTPException(status_code=500, detail="Failed to check inbox")

            standardized_messages = []
            for msg in messages_raw:
                standardized_messages.append({
                    "from": msg.get('fromAddress', {}).get('address', 'Unknown'),
                    "subject": msg.get('subject', 'No Subject'),
                    "date": datetime.fromtimestamp(msg.get('receivedAt')).strftime('%Y-%m-%d %H:%M:%S') if msg.get('receivedAt') else 'N/A',
                    "body": msg.get('text', 'No content available.')
                })
            return {"mailbox": session['email'], "messages": standardized_messages}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_edu_email(self):
        scraper = cloudscraper.create_scraper() # EDU mail doesn't need a proxy
        try:
            response = scraper.post("https://etempmail.com/getEmailAddress", headers={'origin': 'https://etempmail.com'})
            if response.status_code == 200:
                data = json.loads(self.decompress_edu_response(response))
                return data['address'], data['recover_key'], response.cookies.get_dict()
        except Exception:
            return None, None, None
        return None, None, None

    async def check_edu_inbox(self, cookies):
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.post("https://etempmail.com/getInbox", headers={'origin': 'https://etempmail.com'}, cookies=cookies)
            if response.status_code == 200:
                return json.loads(self.decompress_edu_response(response))
        except Exception:
            return []
        return []

    async def generate_edu_email(self):
        email, _, cookies = await self.get_edu_email()
        if not email:
            raise HTTPException(status_code=500, detail="Failed to generate EDU email")
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
async def root(): return {"message": "API is running."}

@app.get("/api/gen")
async def generate_mail(): return await temp_mail_service.generate_temp_mail(False)

@app.get("/api/chk")
async def check_mail(token: str): return await temp_mail_service.check_messages(token)

@app.get("/api/10min/gen")
async def generate_10min_mail(): return await temp_mail_service.generate_temp_mail(True)

@app.get("/api/10min/chk")
async def check_10min_mail(token: str): return await temp_mail_service.check_messages(token)

@app.get("/api/edu/gen")
async def generate_edu_email_endpoint(): return await temp_mail_service.generate_edu_email()

@app.get("/api/edu/chk")
async def check_edu_messages_endpoint(token: str): return await temp_mail_service.check_edu_messages(token)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

