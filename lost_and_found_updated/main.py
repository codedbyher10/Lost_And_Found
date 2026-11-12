# --- 1. Imports ---
import os
import re
import io
import time
import uuid
import threading
import random
import urllib.request
import urllib.parse
import urllib.error
import json
from typing import List, Optional
from pathlib import Path
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader, select_autoescape

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import EmailStr, BaseModel
from PIL import Image
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

# --- 2. Configuration ---
# Set these in your environment / deployment:
#   KNIT_SMTP_EMAIL, KNIT_SMTP_PASSWORD, KNIT_SMTP_SERVER, KNIT_SMTP_PORT
# Emails are sent via the configured SMTP (Gmail app passwords by default).
EMAIL_CONFIG = {
    "SENDER_EMAIL": os.environ.get("KNIT_SMTP_EMAIL", "lostandfoundresponse@gmail.com"),
    "SENDER_PASSWORD": os.environ.get("KNIT_SMTP_PASSWORD", "ujntmefubfgdfvij"),
    "SMTP_SERVER": os.environ.get("KNIT_SMTP_SERVER", "smtp.gmail.com"),
    "SMTP_PORT": int(os.environ.get("KNIT_SMTP_PORT", "587")),
}

# --- Fast2SMS Integration ---
# Sign up at fast2sms.com, get your API Key, and paste it below. 
FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY", "YOUR_FAST2SMS_API_KEY_HERE")


if not EMAIL_CONFIG["SENDER_EMAIL"] or not EMAIL_CONFIG["SENDER_PASSWORD"]:
    print("WARNING: KNIT_SMTP_EMAIL / KNIT_SMTP_PASSWORD not set — claim "
          "notification emails will be skipped until configured.")

# Setup for Jinja2 templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STORAGE_DIR = os.path.join(BASE_DIR, "qdrant_storage")
IMAGES_DIR = os.path.join(BASE_DIR, "uploaded_images")
STATIC_DIR = os.path.join(BASE_DIR, "static")

env = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                   autoescape=select_autoescape(['html', 'xml']))
claim_template = env.get_template('claim_notification.html')

# --- 3. Directory Setup ---
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

print("Loading CLIP model...")
model = SentenceTransformer("clip-ViT-B-32")
print("CLIP model loaded successfully.")

client = QdrantClient(path=STORAGE_DIR)
found_collection = "found_items_collection"
lost_collection = "lost_items_collection"
VALID_COLLECTIONS = {found_collection, lost_collection}


def ensure_collection(collection_name: str):
    try:
        client.get_collection(collection_name)
        print(f"Collection '{collection_name}' already exists.")
    except Exception:
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "image": models.VectorParams(size=512, distance=models.Distance.COSINE),
                "text": models.VectorParams(size=512, distance=models.Distance.COSINE),
            },
        )
        print(f"Collection '{collection_name}' created.")

ensure_collection(found_collection)
ensure_collection(lost_collection)

app = FastAPI(
    title="Lost & Found AI",
    description="API for image-based lost and found item matching with email notifications.",
    version="8.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

Image.MAX_IMAGE_PIXELS = 4096 * 4096

# --- Security limits & helpers ---
MAX_UPLOAD_BYTES = 8 * 1024 * 1024      
MAX_TEXT_LEN = 120                      
MAX_NAME_LEN = 60                       
MAX_PHONE_LEN = 30
ALLOWED_CATEGORIES = {"Electronics", "Clothing", "Pets", "Keys", "Documents",
                      "Bags & Luggage", "Other"}
REDACT = "••••••••"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value, max_len=MAX_TEXT_LEN):
    if value is None:
        return ""
    v = re.sub(r"<[^>]*>", "", str(value))
    v = _CONTROL_CHARS.sub(" ", v)
    v = v.replace("\r", " ").replace("\n", " ")
    v = " ".join(v.split())
    return v[:max_len]


def sanitize_name(value):
    v = clean_text(value, MAX_NAME_LEN)
    return v or "Unnamed"


def sanitize_phone(value):
    v = re.sub(r"[\r\n]", "", str(value or ""))
    v = re.sub(r"[^0-9+()\-. ]", "", v).strip()
    return v[:MAX_PHONE_LEN] or "Not Provided"


def sanitize_coord(value):
    v = str(value or "").strip()
    if not v or v.lower() in ("not provided", "none", "nan", "inf"):
        return "Not Provided"
    try:
        f = float(v)
    except ValueError:
        return "Not Provided"
    if not (f == f) or f in (float("inf"), float("-inf")):
        return "Not Provided"
    return str(f)


def validate_category(value):
    v = clean_text(value, 40)
    return v if v in ALLOWED_CATEGORIES else "Other"


def sanitize_email_header(value):
    return re.sub(r"[\r\n]+", " ", clean_text(value, 200)).strip()


def redact_contact(payload):
    p = dict(payload)
    for key in ("finder_contact_email", "loser_contact_email", "finder_phone", "loser_phone"):
        if key in p and p[key]:
            p[key] = REDACT
    return p

# --- In-memory per-IP rate limiting ---
_RATE_WINDOW = {"write": (8, 90), "lookup": (30, 300), "otp": (10, 60)}  
_rate_log = {}               
_rate_lock = threading.Lock()
_WRITE_PATHS = ("/api/report-found", "/api/report-lost", "/api/claim-item")


def _rate_allowed(key, group):
    limit, window = _RATE_WINDOW[group]
    now = time.monotonic()
    with _rate_lock:
        entry = _rate_log.setdefault((group, key), [])
        sliced = [t for t in entry if now - t < window]
        _rate_log[(group, key)] = sliced
        if len(sliced) >= limit:
            return False
        sliced.append(now)
        return True


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"

    if request.method == "POST":
        path = request.url.path
        if path.startswith("/api/send-otp") or path.startswith("/api/verify-otp"):
            group = "otp"
        elif path.startswith("/api/report") or path in _WRITE_PATHS:
            group = "write"
        elif path in ("/api/get-items-by-email", "/api/get-potential-matches"):
            group = "lookup"
        else:
            group = None
            
        if group and not _rate_allowed(ip, group):
            return JSONResponse(status_code=429, content={"detail": "Too many requests. Please slow down."})

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(self)"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response

# --- 5. Core ML & Helper Functions ---
def get_image_embedding(image_file):
    try:
        image_file.file.seek(0)
        image = Image.open(image_file.file)
        image.load()
        image = image.convert("RGB")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        embedding = model.encode(image)
        return embedding
    except Exception as e:
        print(f"Error generating embedding: {e}")
        return None

def get_text_embedding(text: str):
    try:
        cleaned = " ".join((text or "").strip().split())
        if not cleaned:
            return np.zeros(512)
        return model.encode(cleaned)
    except Exception as e:
        print(f"Error generating text embedding: {e}")
        return np.zeros(512)


def cosine_similarity(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


MATCH_THRESHOLD = 0.8       
MATCH_LIMIT = 6             
VIS_PREFILTER = 0.65        
W_VISUAL = 0.55             
W_TEXT = 0.35               
CATEGORY_BONUS = 0.08       
PREFETCH_FACTOR = 5         


def search_potential_matches(target_collection: str, query_image, query_text,
                             query_category: str, limit: int = MATCH_LIMIT):
    available_filter = models.Filter(must=[
        models.FieldCondition(key="status", match=models.MatchValue(value="available")),
    ])
    response = client.query_points(
        collection_name=target_collection,
        query=query_image,
        query_filter=available_filter,
        limit=limit * PREFETCH_FACTOR,
        score_threshold=VIS_PREFILTER,
        using="image",
    )

    if not response.points:
        return []

    visual_scores = {p.id: p.score for p in response.points}

    candidates = client.retrieve(
        collection_name=target_collection,
        ids=list(visual_scores.keys()),
        with_payload=True,
        with_vectors=True,
    )

    results = []
    for point in candidates:
        payload = point.payload
        vectors = point.vector if isinstance(point.vector, dict) else {}

        text_vec = vectors.get("text")
        if text_vec is None:
            text_vec = np.zeros(512)

        visual_score = visual_scores.get(point.id, 0.0)
        text_score = cosine_similarity(query_text, text_vec)
        category_bonus = CATEGORY_BONUS if (query_category and payload.get("category")
                                            and query_category.strip().lower() == str(payload.get("category")).strip().lower()) else 0.0

        fused_score = W_VISUAL * visual_score + W_TEXT * text_score + category_bonus
        if fused_score < MATCH_THRESHOLD:
            continue

        results.append({
            **payload,
            "score": round(fused_score, 4),
            "visual_score": round(visual_score, 4),
            "text_score": round(text_score, 4),
            "collection": target_collection,
        })

    results.sort(key=lambda r: (-r["score"], r.get("upload_time", "")))
    return results[:limit]


def save_image(image_file: UploadFile, item_id: str):
    try:
        image_file.file.seek(0)
        img = Image.open(image_file.file)
        img.load()
        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88, optimize=True)

        new_filename = f"{item_id}.jpg"
        file_path = os.path.join(IMAGES_DIR, new_filename)
        with open(file_path, "wb") as buffer:
            buffer.write(out.getvalue())
        return f"/images/{new_filename}"
    except Exception as e:
        print(f"Error saving image: {e}")
        return None


def _check_upload_size(image_file: UploadFile):
    try:
        f = image_file.file
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
    except Exception:
        size = MAX_UPLOAD_BYTES + 1
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Image too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

# --- 6. Email Sending Logic ---
def send_claim_notification_email(reporting_item_payload: dict, matched_item_payload: dict):
    found_item_contact = {}
    lost_item_contact = {}

    if reporting_item_payload.get("finder_name"):
        found_item_contact = reporting_item_payload
        lost_item_contact = matched_item_payload
    elif reporting_item_payload.get("loser_name"):
        found_item_contact = matched_item_payload
        lost_item_contact = reporting_item_payload
    else:
        print("CRITICAL: Could not determine item types for email notification.")
        return

    html_content = claim_template.render(
        found_item_name=found_item_contact.get("item_name", "Unknown Found Item"),
        lost_item_name=lost_item_contact.get("item_name", "Unknown Lost Item"),
        finder_name=found_item_contact.get("finder_name", "A Finder"),
        finder_contact_email=found_item_contact.get("finder_contact_email", "Not Provided"),
        finder_phone=found_item_contact.get("finder_phone", "Not Provided"),
        loser_name=lost_item_contact.get("loser_name", "An Owner"),
        loser_contact_email=lost_item_contact.get("loser_contact_email", "Not Provided"),
        loser_phone=lost_item_contact.get("loser_phone", "Not Provided"),
    )

    recipients = []
    if lost_item_contact.get("loser_contact_email"):
        recipients.append(lost_item_contact["loser_contact_email"])
    if found_item_contact.get("finder_contact_email"):
        recipients.append(found_item_contact["finder_contact_email"])

    if not recipients:
        print("WARNING: No valid recipients for claim notification email.")
        return

    if not EMAIL_CONFIG["SENDER_EMAIL"] or not EMAIL_CONFIG["SENDER_PASSWORD"]:
        print("WARNING: Claim matched, but SMTP is not configured — skipping email.")
        return

    subject = "Item Claimed on Lost & Found AI: {} / {}".format(
        sanitize_email_header(found_item_contact.get("item_name")),
        sanitize_email_header(lost_item_contact.get("item_name")))

    msg = MIMEMultipart()
    msg['From'] = EMAIL_CONFIG["SENDER_EMAIL"]
    msg['Subject'] = subject
    msg['To'] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP(EMAIL_CONFIG["SMTP_SERVER"], EMAIL_CONFIG["SMTP_PORT"]) as server:
            server.starttls()
            server.login(EMAIL_CONFIG["SENDER_EMAIL"], EMAIL_CONFIG["SENDER_PASSWORD"])
            server.sendmail(EMAIL_CONFIG["SENDER_EMAIL"], recipients, msg.as_string())
            print(f"SUCCESS: Claim notification email sent for items '{found_item_contact.get('item_name')}' and '{lost_item_contact.get('item_name')}'")
    except Exception as e:
        print(f"CRITICAL: Email sending failed. Error: {e}")

# --- 7. API Endpoints ---

class ClaimRequest(BaseModel):
    reporting_item_id: str
    reporting_item_collection: str
    matched_item_id: str
    matched_item_collection: str

class ItemMatchRequest(BaseModel):
    item_id: str
    collection_name: str

class EmailRequest(BaseModel):
    email: EmailStr


# In-memory temporary storage for OTPs
otp_store = {}

@app.post("/api/send-otp")
async def send_otp(phone: str):
    if not phone.isdigit() or len(phone) != 10:
        raise HTTPException(status_code=400, detail="Invalid 10-digit phone number.")
    
    otp = str(random.randint(100000, 999999))
    otp_store[phone] = otp
    
    # Send via Fast2SMS if API key is provided
    if FAST2SMS_API_KEY and FAST2SMS_API_KEY != "YOUR_FAST2SMS_API_KEY_HERE":
        try:
            url = "https://www.fast2sms.com/dev/bulkV2"
            params = {
                "authorization": FAST2SMS_API_KEY,
                "variables_values": otp,
                "route": "otp",
                "numbers": phone
            }
            querystring = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{url}?{querystring}", method="GET")
            
            with urllib.request.urlopen(req) as response:
                res_data = json.loads(response.read().decode())
                print(f"DEBUG: Fast2SMS Response: {res_data}")
                
        except urllib.error.HTTPError as e:
            # This catches the exact error Fast2SMS returns and sends it to the frontend
            error_response = e.read().decode()
            print(f"CRITICAL: Fast2SMS HTTP Error: {error_response}")
            raise HTTPException(status_code=500, detail=f"Fast2SMS Error: {error_response}")
            
        except Exception as e:
            print(f"CRITICAL: Failed to send SMS via Fast2SMS: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")
    else:
        print(f"DEBUG: No Fast2SMS API Key configured. Generated OTP for {phone} is {otp}")

    return {"message": "OTP sent successfully", "dev_otp": otp}

@app.post("/api/verify-otp")
async def verify_otp(phone: str, otp: str):
    stored_otp = otp_store.get(phone)
    if not stored_otp or stored_otp != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    del otp_store[phone]
    return {"message": "Phone verified successfully"}


@app.post("/api/report-found")
async def report_found_item(
    item_name: str = Form(...), user_name: str = Form(...),
    user_contact_email: EmailStr = Form(...), user_phone: str = Form(...),
    latitude: str = Form(...), longitude: str = Form(...),
    category: str = Form(...), image: UploadFile = File(...)
):
    _check_upload_size(image)
    embedding = get_image_embedding(image)
    if embedding is None:
        raise HTTPException(status_code=400, detail="Could not process image for embedding.")

    item_id = str(uuid.uuid4())
    saved_image_path = save_image(image, item_id)
    if saved_image_path is None:
        raise HTTPException(status_code=500, detail="Could not save image.")

    item_name = clean_text(item_name) or "Unnamed item"
    category = validate_category(category)
    user_name = sanitize_name(user_name)
    user_phone = sanitize_phone(user_phone)
    latitude = sanitize_coord(latitude)
    longitude = sanitize_coord(longitude)

    text_embedding = get_text_embedding(f"{category} {item_name}")

    found_item_payload = {
        "item_id": item_id,
        "item_name": item_name,
        "image_url": saved_image_path,
        "category": category,
        "finder_name": user_name,
        "finder_contact_email": user_contact_email,
        "finder_phone": user_phone,
        "upload_time": time.strftime("%Y-%m-%d"),
        "latitude": latitude,
        "longitude": longitude,
        "status": "available",
        "matched_with_id": None,
        "matched_with_name": None,
    }

    client.upsert(collection_name=found_collection,
                  points=[models.PointStruct(id=item_id,
                                             vector={"image": embedding.tolist(), "text": text_embedding.tolist()},
                                             payload=found_item_payload)])

    potential_matches = search_potential_matches(lost_collection, embedding, text_embedding, category)

    return {
        "reported_item_id": item_id,
        "reported_item_collection": found_collection,
        "reported_item_payload": redact_contact(found_item_payload),
        "potential_matches": [redact_contact(m) for m in potential_matches]
    }


@app.post("/api/report-lost")
async def report_lost_item(
    item_name: str = Form(...), user_name: str = Form(...),
    user_contact_email: EmailStr = Form(...), user_phone: str = Form(...),
    latitude: str = Form(...), longitude: str = Form(...),
    category: str = Form(...), image: UploadFile = File(...)
):
    _check_upload_size(image)
    embedding = get_image_embedding(image)
    if embedding is None:
        raise HTTPException(status_code=400, detail="Could not process image for embedding.")

    item_id = str(uuid.uuid4())
    saved_image_path = save_image(image, item_id)
    if saved_image_path is None:
        raise HTTPException(status_code=500, detail="Could not save image.")

    item_name = clean_text(item_name) or "Unnamed item"
    category = validate_category(category)
    user_name = sanitize_name(user_name)
    user_phone = sanitize_phone(user_phone)
    latitude = sanitize_coord(latitude)
    longitude = sanitize_coord(longitude)

    text_embedding = get_text_embedding(f"{category} {item_name}")

    lost_item_payload = {
        "item_id": item_id,
        "item_name": item_name,
        "image_url": saved_image_path,
        "category": category,
        "loser_name": user_name,
        "loser_contact_email": user_contact_email,
        "loser_phone": user_phone,
        "upload_time": time.strftime("%Y-%m-%d"),
        "latitude": latitude,
        "longitude": longitude,
        "status": "available",
        "matched_with_id": None,
        "matched_with_name": None,
    }

    client.upsert(collection_name=lost_collection,
                  points=[models.PointStruct(id=item_id,
                                             vector={"image": embedding.tolist(), "text": text_embedding.tolist()},
                                             payload=lost_item_payload)])

    potential_matches = search_potential_matches(found_collection, embedding, text_embedding, category)

    return {
        "reported_item_id": item_id,
        "reported_item_collection": lost_collection,
        "reported_item_payload": redact_contact(lost_item_payload),
        "potential_matches": [redact_contact(m) for m in potential_matches]
    }


@app.post("/api/claim-item")
async def claim_item(request: ClaimRequest):
    try:
        if not {request.reporting_item_collection, request.matched_item_collection} <= VALID_COLLECTIONS:
            raise HTTPException(status_code=400, detail="Invalid collection name.")
        if request.reporting_item_collection == request.matched_item_collection:
            raise HTTPException(status_code=400, detail="Items must come from different collections.")

        reporting_item_point = client.retrieve(collection_name=request.reporting_item_collection, ids=[request.reporting_item_id], with_payload=True)
        matched_item_point = client.retrieve(collection_name=request.matched_item_collection, ids=[request.matched_item_id], with_payload=True)

        if not reporting_item_point or not matched_item_point:
            raise HTTPException(status_code=404, detail="One or both items not found.")

        reporting_item_payload = reporting_item_point[0].payload
        matched_item_payload = matched_item_point[0].payload

        if reporting_item_payload.get("status") != "available" or matched_item_payload.get("status") != "available":
            raise HTTPException(status_code=400, detail="One or both items are not available to be claimed.")

        reporting_item_payload["status"] = "claimed"
        reporting_item_payload["matched_with_id"] = request.matched_item_id
        reporting_item_payload["matched_with_name"] = matched_item_payload.get("item_name", "Unknown Item")
        client.set_payload(collection_name=request.reporting_item_collection, payload=reporting_item_payload, points=[request.reporting_item_id])

        matched_item_payload["status"] = "claimed"
        matched_item_payload["matched_with_id"] = request.reporting_item_id
        matched_item_payload["matched_with_name"] = reporting_item_payload.get("item_name", "Unknown Item")
        client.set_payload(collection_name=request.matched_item_collection, payload=matched_item_payload, points=[request.matched_item_id])

        send_claim_notification_email(reporting_item_payload, matched_item_payload)

        return {"message": "Items successfully claimed and owners notified!", "status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to claim item due to server error: {e}")


@app.post("/api/mark-as-returned/{collection_name}/{item_id}")
async def mark_as_returned(collection_name: str, item_id: str):
    if collection_name not in [found_collection, lost_collection]:
        raise HTTPException(status_code=400, detail="Invalid collection name.")
    
    try:
        item_points = client.retrieve(collection_name=collection_name, ids=[item_id], with_payload=True)
        if not item_points:
            raise HTTPException(status_code=404, detail="Item not found.")
        
        item_payload = item_points[0].payload
        
        if item_payload.get("status") != "claimed":
            raise HTTPException(status_code=400, detail="Only claimed items can be marked as returned.")
            
        item_payload["status"] = "returned"
        client.set_payload(collection_name=collection_name, payload=item_payload, points=[item_id])
        
        matched_id = item_payload.get("matched_with_id")
        if matched_id:
            matched_collection = lost_collection if collection_name == found_collection else found_collection
            matched_points = client.retrieve(collection_name=matched_collection, ids=[matched_id], with_payload=True)
            if matched_points:
                matched_payload = matched_points[0].payload
                matched_payload["status"] = "returned"
                client.set_payload(collection_name=matched_collection, payload=matched_payload, points=[matched_id])

        return {"message": "Item and its match have been successfully marked as returned.", "status": "success"}

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


@app.post("/api/get-potential-matches")
async def get_potential_matches_for_item(request: ItemMatchRequest):
    try:
        if request.collection_name not in VALID_COLLECTIONS:
            raise HTTPException(status_code=400, detail="Invalid collection name.")

        item_point = client.retrieve(collection_name=request.collection_name, ids=[request.item_id], with_payload=True, with_vectors=True)
        if not item_point:
            raise HTTPException(status_code=404, detail="Item not found.")

        item_payload = item_point[0].payload
        item_vectors = item_point[0].vector if isinstance(item_point[0].vector, dict) else {}
        item_embedding = item_vectors.get("image")
        item_text = item_vectors.get("text") or np.zeros(512)

        target_collection = lost_collection if request.collection_name == found_collection else found_collection

        potential_matches = search_potential_matches(
            target_collection, item_embedding, item_text, item_payload.get("category", "")
        )

        return {
            "reported_item_id": request.item_id,
            "reported_item_collection": request.collection_name,
            "reported_item_payload": redact_contact(item_payload),
            "potential_matches": [redact_contact(m) for m in potential_matches]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get potential matches due to server error: {e}")


@app.post("/api/get-items-by-email")
async def get_items_by_email(request: EmailRequest):
    email = request.email
    all_items = []
    
    found_filter = models.Filter(must=[
        models.FieldCondition(key="finder_contact_email", match=models.MatchValue(value=email))
    ])
    found_results, _ = client.scroll(collection_name=found_collection, scroll_filter=found_filter, limit=50, with_payload=True)
    for record in found_results:
        item = record.payload
        item["collection"] = found_collection
        all_items.append(item)

    lost_filter = models.Filter(must=[
        models.FieldCondition(key="loser_contact_email", match=models.MatchValue(value=email))
    ])
    lost_results, _ = client.scroll(collection_name=lost_collection, scroll_filter=lost_filter, limit=50, with_payload=True)
    for record in lost_results:
        item = record.payload
        item["collection"] = lost_collection
        all_items.append(item)
        
    return sorted([redact_contact(i) for i in all_items],
                  key=lambda x: x.get('upload_time'), reverse=True)


@app.get("/api/get-found-items")
async def get_found_items():
    response, _ = client.scroll(
        collection_name=found_collection, 
        limit=12, 
        with_payload=True, 
        order_by=models.OrderBy(key="upload_time", direction="desc")
    )
    return [redact_contact(record.payload) for record in response]

@app.get("/api/get-lost-items")
async def get_lost_items():
    response, _ = client.scroll(
        collection_name=lost_collection, 
        limit=12, 
        with_payload=True, 
        order_by=models.OrderBy(key="upload_time", direction="desc")
    )
    return [redact_contact(record.payload) for record in response]


@app.get("/api/item-details/{collection_name}/{item_id}")
async def get_item_details(collection_name: str, item_id: str):
    if collection_name not in [found_collection, lost_collection]:
        raise HTTPException(status_code=400, detail="Invalid collection name")
    try:
        point = client.retrieve(collection_name=collection_name, ids=[item_id], with_payload=True)
        if not point:
            raise HTTPException(status_code=404, detail="Item not found")
        return redact_contact(point[0].payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving item: {e}")


# --- 8. Static File Serving ---
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# --- 9. Run the Server ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)