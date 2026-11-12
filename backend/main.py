import base64
import hashlib
import hmac
import io
import json
import os
import logging
import re
import secrets
import shutil
import smtplib
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from email.message import EmailMessage
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional

import pymupdf as fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from google import genai
from google.genai import types
from PIL import Image, ImageOps

try:  # skan təmizləmə üçün (istəyə bağlı)
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:  # iPhone HEIC/HEIF (istəyə bağlı)
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

load_dotenv()

import db  # .env oxunandan sonra import olunmalıdır (DATABASE_URL üçün)

db.init_db()

# ---------------------------------------------------------------- Ayarlar
ENDPOINT = os.environ["AZURE_DI_ENDPOINT"]
KEY = os.environ["AZURE_DI_KEY"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL")

# APP_PASSWORD təyin olunubsa hesab rejimi qoşulur: hər istifadəçi öz adı və parolu ilə daxil olur.
# Bu dəyər yeni hesab açmaq üçün QEYDİYYAT KODUDUR. Boşdursa giriş tələb olunmur (yalnız lokal işləmə üçün).
APP_PASSWORD = os.getenv("APP_PASSWORD")
RATE_LIMIT = os.getenv("RATE_LIMIT", "20/minute")  # IP başına
AUTH_RATE_LIMIT = os.getenv("AUTH_RATE_LIMIT", "10/minute")  # giriş və qeydiyyat cəhdləri, IP başına
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

MAX_FILES = int(os.getenv("MAX_FILES", "20"))
MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "10"))
MAX_TOTAL_MB = int(os.getenv("MAX_TOTAL_MB", "40"))
# Azure F0 (pulsuz) hər sorğuda yalnız ilk 2 səhifəni oxuyur. Uzun PDF 2 səhifəlik hissələrlə göndərilir.
# S0 (ödənişli) planda bu məhdudiyyət yoxdur: AZURE_PAGES_PER_CALL=0 yazın.
AZURE_PAGES_PER_CALL = int(os.getenv("AZURE_PAGES_PER_CALL", "2"))
LLM_CHUNK_CHARS = int(os.getenv("LLM_CHUNK_CHARS", "6000"))
MAX_XLSX_ROWS = 5000
# Azure Document Intelligence: F0 (pulsuz) plan 4 MB-dan böyük faylı qəbul etmir (InvalidContentLength).
# Bundan böyük skanlar göndərilməzdən əvvəl avtomatik sıxılır. S0 planında AZURE_MAX_MB artırıla bilər.
AZURE_MAX_BYTES = int(float(os.getenv("AZURE_MAX_MB", "3.5")) * 1024 * 1024)
JOB_WORKERS = int(os.getenv("JOB_WORKERS", "2"))          # eyni anda neçə iş emal olunur
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "21600"))      # bitmiş iş bazada nə qədər qalır (saniyə)
SESSION_SECRET = os.getenv("SESSION_SECRET") or ("az-ocr-session|" + KEY)   # giriş nişanlarını imzalamaq üçün
TOKEN_TTL = int(os.getenv("TOKEN_DAYS", "30")) * 86400
# E-poçt (parolun bərpası). Render-in pulsuz planında SMTP portları bağlıdır, ona görə Brevo-nun HTTPS API-si işlədilir.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()                  # Brevo-da təsdiqlənmiş göndərən ünvan
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Azərbaycan OCR").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()                  # istəyə bağlı: öz SMTP serveriniz (pulsuz Render-də işləmir)
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_CONSOLE = os.getenv("MAIL_CONSOLE", "") == "1"             # yalnız lokal test: məktub göndərilmir, terminala yazılır
RESET_TTL = int(os.getenv("RESET_MINUTES", "60")) * 60          # bərpa linki nə qədər etibarlıdır
VERIFY_TTL = int(os.getenv("VERIFY_HOURS", "24")) * 3600        # e-poçt təsdiqi linki nə qədər etibarlıdır
REQUIRE_VERIFIED = os.getenv("REQUIRE_EMAIL_VERIFICATION", "1") != "0"   # məktub xidməti varsa: təsdiqsiz hesab daxil ola bilməz
USER_MONTHLY_PAGES = int(os.getenv("USER_MONTHLY_PAGES", "0"))   # hər hesab üçün aylıq Azure səhifə limiti; 0 = limitsiz
RATE_LIMIT_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "").strip()  # məs. rediss://... ; boşdursa yaddaşda
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()                # boşdursa Sentry söndürülüb
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")          # bərpa linkinin ünvanı (Render-də avtomatik tapılır)
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "100"))        # hər istifadəçi üçün serverdə ən çox neçə tarixçə qeydi
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "2000000"))
SOFFICE_PATH = os.getenv("SOFFICE_PATH", "")              # LibreOffice (soffice) yolu, avtomatik tapılmasa
LO_TIMEOUT = int(os.getenv("LO_TIMEOUT_SECONDS", "120"))
MAX_ACTIVE_JOBS = int(os.getenv("MAX_ACTIVE_JOBS", "6"))  # növbədə + işləyən iş limiti
log = logging.getLogger("az-ocr")

client = DocumentIntelligenceClient(
    endpoint=ENDPOINT,
    credential=AzureKeyCredential(KEY),
)
llm = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ---------------------------------------------------------------- Formatlar
PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif"}
IMAGE_DIRECT_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}  # Azure birbaşa oxuyur
DOCUMENT_EXT = {".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv", ".html", ".htm"}
ALLOWED_EXT = PDF_EXT | IMAGE_EXT | DOCUMENT_EXT

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}
GEMINI_VISION_TYPES = {"application/pdf", "image/jpeg", "image/png"}
RETRYABLE = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL")

SYSTEM_PROMPT = """Sən Azərbaycan dili üzrə OCR nəticələrini düzəldən redaktorsan.
Sənə OCR ilə çıxarılmış mətn (və mümkünsə sənədin şəkli) verilir.
OCR mətnində tipik səhvlər var:
- "ə" hərfi tez-tez "o", "a", "e" və ya "ǝ" kimi səhv oxunur (məs. "Mammadli" → "Məmmədli", "Azerbaycan" → "Azərbaycan", "dastayi" → "dəstəyi")
- BÖYÜK "Ə" hərfi (xüsusilə kiçik, böyük hərflərlə yazılmış başlıq və loqolarda) "O", "Đ", "D", "Ɵ", "3" və ya "E" kimi səhv oxunur (məs. "XİDMOTİ" → "XİDMƏTİ", "VĐ" → "VƏ", "MƏRKƏZ" yerinə "MORKOZ"). Şəkildə Ə görünürsə, onu mütləq Ə kimi yaz.
- BÖYÜK hərflərlə yazılmış mətndə işarələr itir: "Ö"→"O", "Ü"→"U", "Ç"→"C", "Ş"→"S", "Ğ"→"G", "İ"→"I". Şəkildə nöqtə, kökçə və ya yarımdairə görünürsə, hərfi bərpa et.
- "ı", "i", "l", "1", "I" bir-birinə qarışır; "q" ilə "g", "o", "9"; "x" ilə "h", "k"; "j" ilə "i" qarışır. Şəkildəki hərfə uyğun yaz.
- "ı", "İ", "ö", "ü", "ç", "ş", "ğ" hərfləri düşür və ya dəyişir
- loqo, möhür, imza və suvenirlərdən gələn mənasız simvol sətirləri

Qaydalar:
1. Şəkil verilibsə, hərfləri şəkildən yoxla və şəkildə görünən mətni əsas götür. Şəkil yoxdursa, kontekstdən düzəlt.
2. Yalnız OCR səhvlərini düzəlt. Mətnin məzmununu, mənasını və sırasını dəyişmə.
3. Rəqəmləri, tarixləri, nömrələri, e-poçt və veb ünvanlarını şəkildə göründüyü kimi saxla.
4. Mənasız zibil sətirləri (loqo/möhür/imzadan gələn) sil. Əmin deyilsənsə, sətri saxla.
5. İngiliscə hissələrə toxunma.
6. Mətndə olmayan heç bir məlumat əlavə etmə, mətni tamamlama.
7. Abzas və sətir strukturunu saxla.
Cavabda yalnız düzəldilmiş mətni qaytar, heç bir izah, başlıq və ya dırnaq işarəsi yazma."""


# ---------------------------------------------------------------- Tətbiq
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(_app):
    recover_jobs()  # server yenidən başlayanda yarımçıq işlər qaldığı yerdən davam edir
    yield


if SENTRY_DSN:   # xəta izləmə (istəyə bağlı): yalnız SENTRY_DSN təyin olunubsa işə düşür
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("SENTRY_ENV", "production"),
            release=os.getenv("RENDER_GIT_COMMIT") or None,
            send_default_pii=False,          # IP, çərəz və Authorization başlığı göndərilmir
            max_request_body_size="never",   # sorğu gövdəsi (parol, qeydiyyat kodu, yüklənən fayl) heç vaxt göndərilmir
            traces_sample_rate=0.0,          # yalnız xətalar, performans izləmə yoxdur
        )
    except ImportError:
        logging.getLogger("az-ocr").warning("SENTRY_DSN verilib, amma sentry-sdk quraşdırılmayıb.")

limiter = Limiter(key_func=client_ip, storage_uri=RATE_LIMIT_STORAGE_URI or None)
app = FastAPI(title="Azərbaycan dili OCR API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- Giriş (hesablar)
GUEST = {"id": 0, "username": "guest"}      # APP_PASSWORD yoxdursa hamı bu istifadəçidir
USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$")          # @ yoxdur: istifadəçi adı e-poçtla qarışmasın
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, _unb64(digest))
    except Exception:
        return False


_DUMMY_HASH = hash_password("dummy-password")  # mövcud olmayan ad üçün də eyni vaxt sərf olunsun


def _pw_tag(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode("utf-8")).hexdigest()[:10]


def _sign(body: str) -> str:
    return _b64(hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def make_token(user: dict) -> str:
    payload = {"u": user["id"], "e": int(time.time()) + TOKEN_TTL, "v": _pw_tag(user["pw_hash"])}
    body = _b64(json.dumps(payload).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def read_token(token: str):
    """Nişan düzgündürsə {id, username}, əks halda None. Parol dəyişəndə köhnə nişanlar etibarsız olur."""
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(_unb64(body))
        if data["e"] < time.time():
            return None
        user = db.get_user(int(data["u"]))
        if not user or _pw_tag(user["pw_hash"]) != data["v"]:
            return None
        if needs_verification(user):
            return None
        return {"id": user["id"], "username": user["display"], "email": user.get("email"),
                "verified": bool(user.get("email_verified"))}
    except Exception:
        return None


def make_reset_token(user: dict) -> str:
    """Parol bərpası nişanı. Parol dəyişəndə _pw_tag dəyişir, ona görə nişan bir dəfəlikdir."""
    payload = {"r": user["id"], "e": int(time.time()) + RESET_TTL, "v": _pw_tag(user["pw_hash"])}
    body = _b64(json.dumps(payload).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def read_reset_token(token: str):
    """Nişan düzgündürsə istifadəçi sətri, əks halda None. Giriş nişanı ("u") burada keçmir."""
    try:
        body, sig = token.strip().split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(_unb64(body))
        if data["e"] < time.time():
            return None
        user = db.get_user(int(data["r"]))
        if not user or _pw_tag(user["pw_hash"]) != data["v"]:
            return None
        return user
    except Exception:
        return None


def _email_tag(email: str) -> str:
    return hashlib.sha256((email or "").encode("utf-8")).hexdigest()[:10]


def make_verify_token(user: dict) -> str:
    """E-poçt təsdiqi nişanı. E-poçt dəyişərsə nişan etibarsız olur ("m" açarı digər nişanlarla qarışmır)."""
    payload = {"m": user["id"], "e": int(time.time()) + VERIFY_TTL, "t": _email_tag(user.get("email"))}
    body = _b64(json.dumps(payload).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def read_verify_token(token: str):
    try:
        body, sig = token.strip().split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(_unb64(body))
        if data["e"] < time.time():
            return None
        user = db.get_user(int(data["m"]))
        if not user or not user.get("email") or _email_tag(user["email"]) != data["t"]:
            return None
        return user
    except Exception:
        return None


def make_email_change_token(user: dict, new_email: str) -> str:
    """Yeni ünvanın təsdiqi nişanı ("c" açarı). Parol dəyişərsə etibarsız olur, ona görə ələ keçirilmiş hesabın köhnə sorğusu işləmir."""
    payload = {"c": user["id"], "n": new_email, "e": int(time.time()) + VERIFY_TTL, "v": _pw_tag(user["pw_hash"])}
    body = _b64(json.dumps(payload).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def read_email_change_token(token: str):
    """Nişan düzgündürsə (istifadəçi sətri, yeni e-poçt), əks halda None."""
    try:
        body, sig = token.strip().split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(_unb64(body))
        if data["e"] < time.time():
            return None
        user = db.get_user(int(data["c"]))
        if not user or _pw_tag(user["pw_hash"]) != data["v"]:
            return None
        return user, str(data["n"])
    except Exception:
        return None


def mail_mode():
    if BREVO_API_KEY and MAIL_FROM:
        return "brevo"
    if SMTP_HOST and (MAIL_FROM or SMTP_USER):
        return "smtp"
    if MAIL_CONSOLE:
        return "console"
    return None


def verification_required() -> bool:
    """Məktub xidməti işləyirsə, e-poçtu təsdiqlənməmiş hesab daxil ola bilməz."""
    return REQUIRE_VERIFIED and mail_mode() is not None


def needs_verification(user: dict) -> bool:
    """E-poçtu olan, amma hələ təsdiqləməyən hesab. E-poçtsuz (köhnə) hesablara tətbiq olunmur."""
    return verification_required() and bool(user.get("email")) and not user.get("email_verified")


def send_mail(to: str, subject: str, text: str, html: str) -> None:
    """Məktub göndərir; alınmasa istisna qaldırır."""
    mode = mail_mode()
    if mode == "brevo":
        payload = json.dumps({
            "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM}, "to": [{"email": to}],
            "subject": subject, "htmlContent": html, "textContent": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email", data=payload, method="POST",
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json", "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Brevo {e.code}: {e.read()[:300]!r}") from e
    elif mode == "smtp":
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM or SMTP_USER}>"
        msg["To"] = to
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with smtp as s:
            if SMTP_PORT != 465:
                s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    elif mode == "console":
        print(f"\n--- MƏKTUB (yalnız lokal test) → {to}\n{subject}\n\n{text}\n---", flush=True)
    else:
        raise RuntimeError("E-poçt xidməti qoşulmayıb.")


def current_user(request: Request) -> dict:
    """Hesab rejimində Authorization: Bearer <nişan> tələb olunur; əks halda hamı 'guest'dir."""
    if not APP_PASSWORD:
        return GUEST
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user = read_token(token) if token else None
    if not user:
        raise HTTPException(401, "Daxil olmaq lazımdır.")
    return user


def account_user(user: dict = Depends(current_user)) -> dict:
    if not APP_PASSWORD:
        raise HTTPException(403, "Tarixçənin serverdə saxlanması üçün hesab rejimi (APP_PASSWORD) lazımdır.")
    return user


# ---------------------------------------------------------------- Mətn köməkçiləri
def _month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def quota_info(user: dict):
    """Aylıq limit məlumatı; limit söndürülübsə (və ya hesabsız rejimdə) None."""
    if not APP_PASSWORD or not USER_MONTHLY_PAGES or not user.get("id"):
        return None
    used = db.usage_get(user["id"], _month())
    return {"enabled": True, "limit": USER_MONTHLY_PAGES, "used": used,
            "left": max(0, USER_MONTHLY_PAGES - used), "month": _month()}


def quota_error(user: dict):
    q = quota_info(user)
    if q and q["left"] <= 0:
        return f"Aylıq limit ({q['limit']} səhifə) bitib. Növbəti ayın əvvəlində yenilənir."
    return None


def check_quota(user: dict) -> None:
    msg = quota_error(user)
    if msg:
        raise HTTPException(429, msg)


def charge_quota(user: dict, res: dict) -> None:
    """Yalnız Azure-dan keçən səhifələr sayılır (mətn qatlı PDF və ofis sənədləri limitə girmir)."""
    if quota_info(user) is None or "error" in res:
        return
    if "azure" in str(res.get("method") or ""):
        db.usage_add(user["id"], _month(), int(res.get("pages") or 0))


def estimate_azure_pages(items) -> int:
    """Axtarışlı PDF üçün: Azure-a gedəcək təxmini səhifə sayı."""
    n = 0
    for name, data in items:
        ext = os.path.splitext(name or "")[1].lower()
        if ext in PDF_EXT:
            if not extract_text_layer(data)[0]:
                try:
                    n += len(fitz.open(stream=data, filetype="pdf"))
                except Exception:
                    n += 1
        elif ext in IMAGE_EXT:
            n += 1
    return n


def normalize_az(text: str) -> str:
    """OCR-ın səhv Unicode simvollarını düzgün Azərbaycan hərflərinə çevirir."""
    table = {
        "\u01dd": "\u0259",        # ǝ -> ə
        "\u018e": "\u018f",        # Ǝ -> Ə
        "\u0131\u0307": "\u0131",  # ı + kombinasiya nöqtəsi -> ı
        "\u0110": "\u018f",        # Đ -> Ə (kiçik BÖYÜK hərfli yazılarda Ə tez-tez belə oxunur)
        "\u0111": "\u0259",        # đ -> ə
        "\u0258": "\u0259",        # ɘ -> ə
        "i\u0307": "i",             # i + kombinasiya nöqtəsi -> i
        "\u0219": "\u015f",        # ș (Rumın, vergüllü) -> ş
        "\u0218": "\u015e",        # Ș -> Ş
        "\u01e7": "\u011f",        # ǧ -> ğ
        "\u01e6": "\u011e",        # Ǧ -> Ğ
    }
    for bad, good in table.items():
        text = text.replace(bad, good)
    # hərf + ayrıca işarə (o + ¨, c + ¸, g + ˘, I + ˙ ...) -> tək hərf (ö, ç, ğ, İ ...)
    text = unicodedata.normalize("NFC", text)
    # tam BÖYÜK hərfli sözdə kiçik "ə" -> "Ə" (məs. XİDMəTİ -> XİDMƏTİ). Ən azı 2 böyük hərf tələb olunur ki,
    # "Mə..." kimi adi sözlər dəyişməsin.
    return re.sub(r"[^\W\d_]+", _caps_schwa, text)


def _caps_schwa(m) -> str:
    w = m.group(0)
    if "\u0259" not in w:
        return w
    others = [c for c in w if c != "\u0259"]
    if len(others) >= 2 and all(c.isupper() for c in others):
        return w.replace("\u0259", "\u018f")
    return w


def page_to_text(page, y_tol: float = 4.0, col_gap: float = 25.0) -> str:
    """PDF səhifəsindəki sözləri koordinatlara görə sətir-sətir yığır."""
    words = page.get_text("words")  # (x0, y0, x1, y1, söz, blok, sətir, söz_no)
    if not words:
        return ""

    def yc(w):
        return (w[1] + w[3]) / 2

    words.sort(key=lambda w: (yc(w), w[0]))
    rows, current, current_y = [], [words[0]], yc(words[0])
    for w in words[1:]:
        if abs(yc(w) - current_y) <= y_tol:
            current.append(w)
        else:
            rows.append(current)
            current, current_y = [w], yc(w)
    rows.append(current)

    lines = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        parts = [row[0][4]]
        for prev, w in zip(row, row[1:]):
            parts.append(("    " if w[0] - prev[2] > col_gap else " ") + w[4])
        lines.append("".join(parts))
    return "\n".join(lines)


def extract_text_layer(data: bytes):
    """Rəqəmsal PDF-dirsə (mətn, səhifə_sayı), skandırsa (None, səhifə_sayı)."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None, 0
    pages_count = len(doc)
    page_texts, scanned_page = [], False
    for p in doc:
        t = page_to_text(p)
        page_texts.append(t)
        # mətni olmayan, amma şəkli olan səhifə = skan; bütün sənəd OCR-a verilir
        if len(t.strip()) < 30 and p.get_images():
            scanned_page = True
    doc.close()
    text = "\n\n".join(page_texts).strip()
    # qırıq şrift kodlaması (\ufffd və ya xüsusi istifadə simvolları): mətn qatına etibar olmaz, OCR edilir
    bad = sum(1 for ch in text if ch == "\ufffd" or "\ue000" <= ch <= "\uf8ff")
    garbled = bool(text) and bad / len(text) > 0.01
    if pages_count and len(text) >= 30 * pages_count and not scanned_page and not garbled:
        return text, pages_count
    return None, pages_count


# ---------------------------------------------------------------- Sənəd formatları (OCR-sız)
def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1254"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def read_docx(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    parts = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, doc).text
            if text.strip():
                parts.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                parts.append("\t".join(c.text.strip() for c in row.cells))
    parts += _docx_extra_text(doc, data)
    return "\n".join(parts)


def _docx_extra_text(doc, data: bytes):
    """Əsas mətndə olmayan hissələr: başlıq/altlıq, mətn qutuları, izahlar (footnote/endnote)."""
    import zipfile
    from xml.etree import ElementTree as ET
    from docx.oxml.ns import qn

    seen, hf, boxes, notes = set(), [], [], []

    def add(bucket, t):
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            bucket.append(t)

    for sec in doc.sections:
        for part in (sec.header, sec.footer, sec.first_page_header, sec.first_page_footer,
                     sec.even_page_header, sec.even_page_footer):
            try:
                for p in part.paragraphs:
                    add(hf, p.text)
                for tb in part.tables:
                    for row in tb.rows:
                        add(hf, "\t".join(c.text.strip() for c in row.cells))
            except Exception:
                continue
    for tx in doc.element.body.iter(qn("w:txbxContent")):
        for p in tx.iter(qn("w:p")):
            add(boxes, "".join(t.text or "" for t in p.iter(qn("w:t"))))
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
                if name not in z.namelist():
                    continue
                for fn in ET.fromstring(z.read(name)).iter(W + tag):
                    if fn.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
                        continue
                    add(notes, " ".join("".join(t.text or "" for t in p.iter(W + "t")) for p in fn.iter(W + "p")))
    except Exception:
        pass
    out = []
    for title, bucket in (("Başlıq/altlıq", hf), ("Mətn qutuları", boxes), ("İzahlar", notes)):
        if bucket:
            out.append(f"--- {title} ---")
            out += bucket
    return out


def read_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"--- Slayd {i} ---")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append("\t".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def read_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"--- {ws.title} ---")
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n >= MAX_XLSX_ROWS:
                parts.append(f"... (yalnız ilk {MAX_XLSX_ROWS} sətir göstərilir)")
                break
            if any(c is not None for c in row):
                parts.append("\t".join("" if c is None else str(c) for c in row))
    wb.close()
    return "\n".join(parts)


class _HtmlText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "table",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_html(data: bytes) -> str:
    parser = _HtmlText()
    parser.feed(decode_text(data))
    out = []
    for ln in (ln.strip() for ln in "".join(parser.parts).splitlines()):
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def document_pages(ext: str, data: bytes):
    """Sənədin səhifə sayı: DOCX üçün Word-ün özünün yazdığı say, PPTX üçün slayd sayı, XLSX üçün vərəq sayı.
    Müəyyən edilə bilmirsə None (təxmin uydurulmur)."""
    try:
        if ext == ".docx":
            import zipfile, re as _re
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("docProps/app.xml").decode("utf-8", "ignore")
            m = _re.search(r"<Pages>\s*(\d+)\s*</Pages>", xml)
            return int(m.group(1)) if m and int(m.group(1)) > 0 else None
        if ext == ".pptx":
            from pptx import Presentation
            return len(Presentation(io.BytesIO(data)).slides) or None
        if ext == ".xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data), read_only=True)
            n = len(wb.worksheets)
            wb.close()
            return n or None
    except Exception:
        pass
    return None


def extract_document(ext: str, data: bytes) -> str:
    if ext == ".docx":
        return read_docx(data)
    if ext == ".pptx":
        return read_pptx(data)
    if ext == ".xlsx":
        return read_xlsx(data)
    if ext in (".html", ".htm"):
        return read_html(data)
    return decode_text(data)  # txt, md, csv


# ---------------------------------------------------------------- Sənəd formatlarından cədvəllər
def _cell_text(c) -> str:
    if c is None:
        return ""
    if isinstance(c, float) and c.is_integer():
        return str(int(c))
    return str(c).replace("\n", " ").strip()


def _clean_grid(rows):
    """Boş sətirləri və boş son sütunları atır, sətirləri eyni uzunluğa gətirir."""
    rows = [[_cell_text(c) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max((i + 1 for r in rows for i, c in enumerate(r) if c), default=0)
    return [(r + [""] * width)[:width] for r in rows[:MAX_XLSX_ROWS]]


def _xlsx_tables(data: bytes):
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        grid = _clean_grid(ws.iter_rows(values_only=True))
        if grid:
            out.append(grid)
    wb.close()
    return out


def _docx_tables(data: bytes):
    from docx import Document

    doc = Document(io.BytesIO(data))
    return [g for g in (_clean_grid([[c.text for c in r.cells] for r in t.rows]) for t in doc.tables) if g]


def _pptx_tables(data: bytes):
    from pptx import Presentation

    out = []
    for slide in Presentation(io.BytesIO(data)).slides:
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                g = _clean_grid([[c.text for c in r.cells] for r in shape.table.rows])
                if g:
                    out.append(g)
    return out


def _csv_tables(data: bytes):
    import csv

    text = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    g = _clean_grid(list(csv.reader(io.StringIO(text), dialect)))
    return [g] if g else []


class _HtmlTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None})
        elif self.stack and tag == "tr":
            self.stack[-1]["row"] = []
        elif self.stack and tag in ("td", "th"):
            self.stack[-1]["cell"] = []

    def handle_endtag(self, tag):
        if not self.stack:
            return
        t = self.stack[-1]
        if tag in ("td", "th") and t["cell"] is not None and t["row"] is not None:
            t["row"].append(" ".join("".join(t["cell"]).split()))
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self.tables.append(self.stack.pop()["rows"])

    def handle_data(self, data):
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(data)


def _html_tables(data: bytes):
    p = _HtmlTables()
    p.feed(decode_text(data))
    return [g for g in (_clean_grid(t) for t in p.tables) if g]


def extract_document_tables(ext: str, data: bytes):
    """Sənəd formatlarındakı cədvəlləri 2D siyahı kimi qaytarır (OCR-sız, pulsuz)."""
    try:
        if ext == ".xlsx":
            return _xlsx_tables(data)
        if ext == ".docx":
            return _docx_tables(data)
        if ext == ".pptx":
            return _pptx_tables(data)
        if ext == ".csv":
            return _csv_tables(data)
        if ext in (".html", ".htm"):
            return _html_tables(data)
    except Exception:
        pass  # cədvəl çıxarmaq alınmasa da mətn qaytarılır
    return []


# ---------------------------------------------------------------- Şəkil: çevirmə və skan təmizləmə
def to_jpeg(data: bytes) -> bytes:
    """Şəkli JPEG-ə çevirir (WEBP/GIF/HEIC üçün), telefon fırlanması düzəldilir."""
    img = Image.open(io.BytesIO(data))
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    img = ImageOps.exif_transpose(img).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _rotate(img, angle, border=cv2.BORDER_REPLICATE if cv2 else None):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=border)


def _order_points(pts):
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    rect[0], rect[2] = pts[np.argmin(s)], pts[np.argmax(s)]   # sol-yuxarı, sağ-aşağı
    rect[1], rect[3] = pts[np.argmin(d)], pts[np.argmax(d)]   # sağ-yuxarı, sol-aşağı
    return rect


def _find_document(img):
    """Şəkildə sənədin 4 küncünü tapır, tapılmasa None."""
    h, w = img.shape[:2]
    scale = 700.0 / max(h, w) if max(h, w) > 700 else 1.0
    small = cv2.resize(img, None, fx=scale, fy=scale) if scale < 1 else img
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    total = small.shape[0] * small.shape[1]
    for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        area = cv2.contourArea(approx)
        if len(approx) == 4 and cv2.isContourConvex(approx) and 0.25 * total < area < 0.98 * total:
            return approx.reshape(4, 2).astype("float32") / scale
    return None


def _warp(img, pts):
    tl, tr, br, bl = _order_points(pts)
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if width < 200 or height < 200:
        return img
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), dst)
    return cv2.warpPerspective(img, m, (width, height), flags=cv2.INTER_CUBIC)


def _estimate_skew(gray) -> float:
    """Proyeksiya profili ilə əyriliyi (dərəcə) tapır."""
    h, w = gray.shape
    s = 600.0 / max(h, w)
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    def score(angle):
        r = _rotate(bw, angle, cv2.BORDER_CONSTANT)
        return float(np.var(r.sum(axis=1, dtype=np.float64)))

    coarse = max(np.arange(-10, 10.1, 1.0), key=score)
    best = max(np.arange(coarse - 1, coarse + 1.01, 0.2), key=score)
    return float(best) if score(best) > score(0.0) * 1.05 else 0.0


def enhance_image(data: bytes) -> bytes:
    """Sənəd şəklini təmizləyir: kənarları kəs, perspektivi düzəlt, kölgəni sil, əyriliyi düzəlt."""
    pil = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

    longest = max(img.shape[:2])
    if longest > 3000:
        f = 3000.0 / longest
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)

    corners = _find_document(img)
    if corners is not None:
        img = _warp(img, corners)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bg = cv2.medianBlur(cv2.dilate(gray, np.ones((7, 7), np.uint8)), 21)  # kölgə/fon
    norm = cv2.divide(gray, bg, scale=255)
    norm = cv2.normalize(norm, None, 0, 255, cv2.NORM_MINMAX)

    angle = _estimate_skew(norm)
    if angle:
        norm = _rotate(norm, angle)

    ok, buf = cv2.imencode(".jpg", norm, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError("JPEG kodlaşdırması alınmadı")
    return buf.tobytes()


def prepare_image(ext: str, data: bytes, enhance: bool):
    """(bayt, mime, xəbərdarlıqlar) qaytarır."""
    warnings = []
    if enhance:
        if cv2 is None:
            warnings.append("OpenCV quraşdırılmayıb, skan təmizləmə keçildi.")
        else:
            try:
                return enhance_image(data), "image/jpeg", warnings
            except Exception as e:
                warnings.append(f"Skan təmizləmə alınmadı: {e}")
    if ext not in IMAGE_DIRECT_EXT:
        try:
            return to_jpeg(data), "image/jpeg", warnings
        except Exception as e:
            raise HTTPException(
                400, f"Şəkil çevrilə bilmədi ({e}). HEIC üçün pillow-heif quraşdırılmalıdır."
            )
    return data, MIME_BY_EXT[ext], warnings


# ---------------------------------------------------------------- Azure OCR
# Böyük skanı Azure limitinə sığacaq qədər sıxır (PDF səhifələri JPEG-ə çevrilir, ölçü tədricən azalır)
_LADDER = ((3000, 85), (2600, 80), (2200, 75), (1800, 70), (1500, 65), (1200, 60))

def _shrink_image(data, limit):
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    longest = max(img.size)
    out = data
    for target, q in _LADDER:
        im = img if longest <= target else img.resize(
            (round(img.width * target / longest), round(img.height * target / longest)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        out = buf.getvalue()
        if len(out) <= limit:
            break
    return out

def _shrink_pdf(data, limit):
    src = fitz.open(stream=data, filetype="pdf")
    out_bytes = data
    try:
        for target, q in _LADDER:
            out = fitz.open()
            for page in src:
                r = page.rect
                scale = min(target / max(r.width, r.height), 4.0)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                np_ = out.new_page(width=r.width, height=r.height)
                np_.insert_image(np_.rect, stream=pix.tobytes("jpeg", jpg_quality=q))
            out_bytes = out.tobytes(garbage=4, deflate=True)
            out.close()
            if len(out_bytes) <= limit:
                break
    finally:
        src.close()
    return out_bytes

def shrink_for_azure(data, limit=None):
    limit = limit or AZURE_MAX_BYTES
    if len(data) <= limit:
        return data
    try:
        small = _shrink_pdf(data, limit) if data[:5] == b"%PDF-" else _shrink_image(data, limit)
    except Exception:
        return data
    return small if len(small) < len(data) else data


def _analyze_once(data: bytes, model: str):
    """Bir Azure sorğusu. Əvvəl locale='az' ilə, alınmasa onsuz sınayır. Böyük fayl əvvəlcə sıxılır."""
    data = shrink_for_azure(data)
    try:
        poller = client.begin_analyze_document(
            model, body=data, content_type="application/octet-stream", locale="az"
        )
        return poller.result()
    except HttpResponseError as e:
        if "InvalidContentLength" in str(e):  # locale ilə əlaqəsi yoxdur, təkrar cəhd mənasızdır
            data = shrink_for_azure(data, limit=1024 * 1024)
        poller = client.begin_analyze_document(
            model, body=data, content_type="application/octet-stream"
        )
        return poller.result()


def _combine_results(results):
    from types import SimpleNamespace
    return SimpleNamespace(
        pages=[p for r in results for p in (r.pages or [])],
        tables=[t for r in results for t in (r.tables or [])],
        content="\n".join(r.content or "" for r in results),
    )


def run_azure_ocr(data: bytes, model: str = "prebuilt-read"):
    """PDF AZURE_PAGES_PER_CALL səhifəlik hissələrə bölünüb ayrı-ayrı oxunur, nəticələr birləşdirilir."""
    n = AZURE_PAGES_PER_CALL
    if n > 0 and data[:5] == b"%PDF-":
        try:
            src = fitz.open(stream=data, filetype="pdf")
        except Exception:
            return _analyze_once(data, model)
        total = len(src)
        if total > n:
            try:
                results = []
                for start in range(0, total, n):
                    part = fitz.open()
                    part.insert_pdf(src, from_page=start, to_page=min(start + n, total) - 1)
                    chunk = part.tobytes(garbage=3, deflate=True)
                    part.close()
                    results.append(_analyze_once(chunk, model))
                return _combine_results(results)
            finally:
                src.close()
        src.close()
    return _analyze_once(data, model)


def extract_tables(result):
    """Azure cədvəllərini 2D siyahı kimi qaytarır."""
    out = []
    for t in result.tables or []:
        grid = [[""] * t.column_count for _ in range(t.row_count)]
        for c in t.cells or []:
            if c.row_index < t.row_count and c.column_index < t.column_count:
                text = (c.content or "").replace("\n", " ").strip()
                grid[c.row_index][c.column_index] = normalize_az(text)
        out.append(grid)
    return out


# ---------------------------------------------------------------- Gemini ilə düzəliş
def _truncated(resp) -> bool:
    try:
        return "MAX_TOKENS" in str(resp.candidates[0].finish_reason)
    except Exception:
        return False


def _split_text(text: str, size: int):
    """Mətni sətir sərhədlərində ~size simvolluq hissələrə bölür."""
    chunks, cur, n = [], [], 0
    for line in text.split("\n"):
        if cur and n + len(line) + 1 > size:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _llm_generate(contents, config, models, errors):
    """(mətn, kəsilib_mi, model) qaytarır; alınmazsa (None, False, None). 503/429-da təkrar cəhd + ehtiyat model."""
    for model_name in models:
        for attempt in range(3):
            try:
                resp = llm.models.generate_content(model=model_name, contents=contents, config=config)
                fixed = (resp.text or "").strip()
                if not fixed:
                    errors.append(f"{model_name}: boş cavab")
                    break
                return fixed, _truncated(resp), model_name
            except Exception as e:
                errors.append(f"{model_name} (cəhd {attempt + 1}): {str(e)[:200]}")
                if any(code in str(e) for code in RETRYABLE) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
    return None, False, None


def fix_with_llm(raw_text: str, file_bytes: bytes = None, mime_type: str = None):
    """(düzəldilmiş_mətn, xəta, rejim). Cavab kəsilərsə mətn hissələrə bölünüb təkrar düzəldilir."""
    if llm is None:
        return raw_text, "GEMINI_API_KEY təyin olunmayıb", None
    if not raw_text.strip() and not file_bytes:
        return raw_text, None, None

    contents, mode = [], "text-only"
    if file_bytes and mime_type in GEMINI_VISION_TYPES:
        contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        mode = "vision+text"
    contents.append("Azure OCR nəticəsi (səhvləri ola bilər):\n\n" + raw_text)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT, temperature=0, max_output_tokens=65536
    )
    models = [LLM_MODEL] + ([LLM_FALLBACK_MODEL] if LLM_FALLBACK_MODEL else [])
    errors = []

    fixed, cut, used = _llm_generate(contents, config, models, errors)
    if fixed is not None and not cut:
        return fixed, None, f"{mode} ({used})"
    if fixed is None:
        log.warning("LLM düzəlişi alınmadı: %s", " || ".join(errors))
        return raw_text, "LLM xətası: " + " || ".join(errors), mode

    # cavab limitə çatıb kəsilib: mətn hissələrə bölünür (şəkilsiz), alınmayan hissə xam qalır
    pieces, failed = [], 0
    for chunk in _split_text(raw_text, LLM_CHUNK_CHARS):
        f, c, _ = _llm_generate(["Azure OCR nəticəsi (səhvləri ola bilər):\n\n" + chunk], config, models, errors)
        if f is None or c:
            pieces.append(chunk)
            failed += 1
        else:
            pieces.append(f)
    err = f"{failed} hissə LLM ilə düzəldilmədi, xam mətn saxlanıldı." if failed else None
    return "\n".join(pieces), err, f"text-only, hissə-hissə ({used})"


# ---------------------------------------------------------------- Əsas emal
def direct_response(filename, method, pages, text, tables=None):
    # Mətn birbaşa faylın özündən götürülür (OCR yoxdur), tanıma qeyri-müəyyənliyi də yoxdur: etibarlılıq 1.0
    return {
        "filename": filename, "method": method, "pages": pages,
        "avg_confidence": 1.0, "llm_fixed": False, "llm_mode": None, "llm_error": None,
        "raw_text": text, "text": text, "tables": tables or [], "warnings": [],
    }


def process_bytes(filename: str, data: bytes, fix: bool = True,
                  enhance: bool = False, tables: bool = False) -> dict:
    """Bir faylı emal edir. evaluate.py də bunu çağırır."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            f"{filename}: dəstəklənməyən format. Dəstəklənənlər: PDF, JPG, PNG, BMP, TIFF, "
            "WEBP, GIF, HEIC, DOCX, PPTX, XLSX, TXT, MD, CSV, HTML.",
        )
    if len(data) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"{filename}: fayl {MAX_SIZE_MB} MB-dan böyük ola bilməz.")

    # Sənəd formatları: OCR-sız
    if ext in DOCUMENT_EXT:
        try:
            text = extract_document(ext, data)
        except Exception as e:
            raise HTTPException(400, f"{filename}: fayl oxuna bilmədi ({e})")
        if not text.strip():
            raise HTTPException(422, f"{filename}: faylda mətn tapılmadı.")
        return direct_response(filename, "document-text", document_pages(ext, data), text,
                               tables=extract_document_tables(ext, data))

    warnings = []
    if ext in PDF_EXT:
        layer_text, layer_pages = extract_text_layer(data)
        if layer_text:
            return direct_response(filename, "pdf-text-layer", layer_pages, layer_text)
        mime = "application/pdf"
    else:
        data, mime, warnings = prepare_image(ext, data, enhance)

    try:
        result = run_azure_ocr(data, "prebuilt-layout" if tables else "prebuilt-read")
    except HttpResponseError as e:
        raise HTTPException(502, f"Azure xətası: {e.message}")

    lines = [ln.content for pg in (result.pages or []) for ln in (pg.lines or [])]
    raw_text = normalize_az("\n".join(lines) if lines else (result.content or ""))

    confs = [w.confidence for pg in (result.pages or []) for w in (pg.words or [])
             if w.confidence is not None]
    avg_conf = round(sum(confs) / len(confs), 3) if confs else None

    text, llm_error, llm_mode = raw_text, None, None
    if fix:
        text, llm_error, llm_mode = fix_with_llm(raw_text, file_bytes=data, mime_type=mime)
        text = normalize_az(text)

    return {
        "filename": filename, "method": "azure-ocr",
        "pages": len(result.pages or []), "avg_confidence": avg_conf,
        "llm_fixed": bool(fix and llm_error is None), "llm_mode": llm_mode, "llm_error": llm_error,
        "raw_text": raw_text, "text": text,
        "tables": extract_tables(result) if tables else [], "warnings": warnings,
    }


def merge_results(results: list, name: str) -> dict:
    ok = [r for r in results if "error" not in r]
    bad = [r for r in results if "error" in r]
    if not ok:
        return {"filename": name, "error": "; ".join(r["error"] for r in bad) or "Nəticə yoxdur"}
    confs = [r["avg_confidence"] for r in ok if r.get("avg_confidence") is not None]
    llm_errors = [r["llm_error"] for r in ok if r.get("llm_error")]
    warnings = [w for r in ok for w in r.get("warnings", [])]
    warnings += [f'{r["filename"]}: {r["error"]}' for r in bad]
    return {
        "filename": name,
        "method": "+".join(dict.fromkeys(r["method"] for r in ok)),
        "pages": sum(r.get("pages") or 0 for r in ok) or None,
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "llm_fixed": any(r.get("llm_fixed") for r in ok) and not llm_errors,
        "llm_mode": None,
        "llm_error": " || ".join(llm_errors) or None,
        "raw_text": "\n\n".join(r["raw_text"] for r in ok),
        "text": "\n\n".join(r["text"] for r in ok),
        "tables": [t for r in ok for t in r.get("tables", [])],
        "warnings": warnings,
    }


def read_uploads(files: List[UploadFile]):
    if not files:
        raise HTTPException(400, "Fayl göndərilməyib.")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Bir dəfəyə ən çox {MAX_FILES} fayl.")
    items = [(f.filename or "fayl", f.file.read()) for f in files]
    if sum(len(d) for _, d in items) > MAX_TOTAL_MB * 1024 * 1024:
        raise HTTPException(400, f"Ümumi ölçü {MAX_TOTAL_MB} MB-dan çox ola bilməz.")
    return items


# ---------------------------------------------------------------- Arxa plan işləri (asinxron emal)
# İşlər və yüklənmiş fayllar bazada saxlanılır. Server yenidən başlasa, yarımçıq işlər qaldığı fayldan davam edir.
_executor = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="ocr")


def _run_job(job_id: str):
    try:
        job = db.get_job(job_id)
        if not job or job["status"] in ("done", "cancelled", "failed"):
            return
        db.update_job(job_id, status="running")
        while not db.job_cancelled(job_id):
            nxt = db.claim_next_file(job_id)
            if nxt is None:
                break
            idx, name, data = nxt
            owner = {"id": job["user_id"]}
            limit_msg = quota_error(owner)
            if limit_msg:   # limit iş davam edərkən bitdi: qalan fayllar işlənmir
                res = {"filename": name, "error": f"{name}: {limit_msg}"}
            else:
                try:
                    res = process_bytes(name, data, fix=job["fix"], enhance=job["enhance"], tables=job["tables"])
                    charge_quota(owner, res)
                except HTTPException as e:
                    res = {"filename": name, "error": str(e.detail)}
                except Exception:  # gözlənilməyən xəta bütün işi dayandırmasın
                    log.exception("Fayl emal edilmədi: %s", name)
                    res = {"filename": name, "error": f"{name}: emal zamanı gözlənilməyən xəta."}
            db.finish_file(job_id, idx, res)
        if db.job_cancelled(job_id):
            db.cancel_job_final(job_id)
            return
        results = db.job_results_list(job_id)
        if job["merge"] and len(results) > 1:
            results = [merge_results(results, f"birləşdirilmiş ({len(results)} səhifə)")]
        db.complete_job(job_id, results)
    except Exception as e:
        log.exception("İş uğursuz oldu: %s", job_id)
        try:
            db.update_job(job_id, status="failed", error=str(e)[:300])
        except Exception:
            pass


def recover_jobs():
    try:
        db.purge_jobs(JOB_TTL)
        for job_id in db.unfinished_job_ids():
            _executor.submit(_run_job, job_id)
    except Exception:
        log.exception("Yarımçıq işlər bərpa edilmədi")


# ---------------------------------------------------------------- Axtarışlı PDF
FONT_CANDIDATES = [
    os.getenv("PDF_FONT", ""),
    os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf"),
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


def load_font():
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return fitz.Font(fontfile=path), None
            except Exception:
                continue
    return fitz.Font("helv"), (
        "Unicode şrift tapılmadı, ə hərfi axtarışda düzgün olmaya bilər. "
        "PDF_FONT mühit dəyişəni ilə .ttf yolunu göstərin."
    )


def add_text_layer(page, az_page, font):
    """Skan şəklinin üstünə Azure koordinatları ilə görünməz mətn qatı əlavə edir."""
    if not az_page.width or not az_page.height:
        return
    sx = page.rect.width / az_page.width
    sy = page.rect.height / az_page.height
    for line in az_page.lines or []:
        poly = line.polygon or []
        text = normalize_az((line.content or "").strip())
        if len(poly) < 8 or not text:
            continue
        xs, ys = poly[0::2], poly[1::2]
        x0, x1 = min(xs) * sx, max(xs) * sx
        y0, y1 = min(ys) * sy, max(ys) * sy
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        fs = max(4.0, h * 0.8)
        natural = font.text_length(text, fontsize=fs)
        if natural <= 0:
            continue
        k = min(5.0, max(0.2, w / natural))
        pos = fitz.Point(x0, y1 - h * 0.2)
        tw = fitz.TextWriter(page.rect)
        tw.append(pos, text, font=font, fontsize=fs)
        tw.write_text(page, render_mode=3, morph=(pos, fitz.Matrix(k, 1)))  # 3 = görünməz


_lo_lock = threading.Lock()  # LibreOffice eyni anda bir çevirmə aparır (yaddaşa qənaət)


def find_soffice():
    """LibreOffice-in soffice faylını tapır, yoxdursa None."""
    candidates = [SOFFICE_PATH, shutil.which("soffice") or "", shutil.which("libreoffice") or "",
                  r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                  "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                  "/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice"]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def docx_to_pdf(data: bytes) -> bytes:
    """DOCX-i LibreOffice ilə PDF-ə çevirir. PDF-də mətn artıq real mətn qatıdır, OCR lazım deyil."""
    soffice = find_soffice()
    if not soffice:
        raise HTTPException(
            503,
            "DOCX-dən axtarışlı PDF üçün LibreOffice lazımdır. Kompüterinizdə quraşdırın "
            "(libreoffice.org) və serveri yenidən başladın. Serverdə Docker imicinə daxildir.",
        )
    with _lo_lock, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        src = os.path.join(tmp, "input.docx")
        with open(src, "wb") as f:
            f.write(data)
        profile = Path(tmp, "profile").as_uri()  # hər çevirmə üçün ayrı profil: kilid toqquşması olmur
        cmd = [soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
               f"-env:UserInstallation={profile}", "--convert-to", "pdf", "--outdir", tmp, src]
        try:
            subprocess.run(cmd, capture_output=True, timeout=LO_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "DOCX PDF-ə çevrilərkən vaxt bitdi. Daha kiçik fayl sınayın.")
        out = os.path.join(tmp, "input.pdf")
        if not os.path.exists(out):
            raise HTTPException(500, "DOCX PDF-ə çevrilə bilmədi (fayl zədəli ola bilər).")
        with open(out, "rb") as f:
            return f.read()


def build_searchable_pdf(items, enhance: bool):
    font, font_warning = load_font()
    out = fitz.open()
    for filename, data in items:
        ext = os.path.splitext(filename)[1].lower()
        if ext in PDF_EXT:
            src = fitz.open(stream=data, filetype="pdf")
            first = len(out)
            out.insert_pdf(src)
            if not extract_text_layer(data)[0]:  # skan PDF: OCR et
                result = run_azure_ocr(data)
                for i, az_page in enumerate(result.pages or []):
                    if first + i < len(out):
                        add_text_layer(out[first + i], az_page, font)
            src.close()
        elif ext in IMAGE_EXT:
            warns = []
            if enhance and cv2 is not None:
                try:
                    jpg = enhance_image(data)
                except Exception:
                    jpg = to_jpeg(data)
            else:
                jpg = to_jpeg(data)
            width, height = Image.open(io.BytesIO(jpg)).size
            page = out.new_page(width=width * 0.75, height=height * 0.75)
            page.insert_image(page.rect, stream=jpg)
            result = run_azure_ocr(jpg)
            if result.pages:
                add_text_layer(page, result.pages[0], font)
        elif ext == ".docx":
            converted = fitz.open(stream=docx_to_pdf(data), filetype="pdf")
            out.insert_pdf(converted)
            converted.close()
        else:
            raise HTTPException(400, f"{filename}: axtarışlı PDF yalnız PDF, şəkil və DOCX üçündür.")
    if not len(out):
        raise HTTPException(400, "PDF yaradıla bilmədi.")
    return out.tobytes(garbage=3, deflate=True), font_warning


# ---------------------------------------------------------------- API
class DocxIn(BaseModel):
    text: str


class XlsxIn(BaseModel):
    tables: List[List[List[str]]]


class RegisterIn(BaseModel):
    username: str
    email: str = ""
    password: str
    invite: str = ""


class ForgotIn(BaseModel):
    login: str          # e-poçt və ya istifadəçi adı


class ResetIn(BaseModel):
    token: str
    password: str


class VerifyIn(BaseModel):
    token: str


class ChangeEmailIn(BaseModel):
    password: str
    email: str


class ChangePasswordIn(BaseModel):
    current: str
    new: str


class DeleteAccountIn(BaseModel):
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class HistIn(BaseModel):
    ts: int
    label: str = Field("Adsız", max_length=300)
    fixed: str = ""
    raw: str = ""
    method: str = ""
    pages: Optional[int] = None
    conf: Optional[float] = None
    llm: bool = False
    tables: List[List[List[str]]] = []
    warnings: List[str] = []
    llmError: Optional[str] = None


@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def healthz():
    """UptimeRobot üçün yüngül yoxlama. Bazaya toxunmur: yoxsa Neon heç vaxt yuxuya getməz və pulsuz saatlar bitər."""
    return {"ok": True}


@app.get("/api/verify", dependencies=[Depends(current_user)])
def verify():
    """Giriş nişanını yoxlayır: OCR işlətmir, sorğu limitinə düşmür."""
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"auth_required": bool(APP_PASSWORD), "accounts": bool(APP_PASSWORD), "max_files": MAX_FILES,
            "max_mb": MAX_SIZE_MB, "opencv": cv2 is not None, "docx_pdf": find_soffice() is not None,
            "mail": bool(APP_PASSWORD) and mail_mode() is not None,
            "verify_required": bool(APP_PASSWORD) and verification_required()}


def _public_user(row: dict) -> dict:
    return {"id": row["id"], "username": row["display"], "email": row.get("email"),
            "verified": bool(row.get("email_verified"))}


@app.post("/api/auth/register")
@limiter.limit(AUTH_RATE_LIMIT)
def register(request: Request, body: RegisterIn, background: BackgroundTasks):
    """Yeni hesab. Qeydiyyat kodu APP_PASSWORD-dir."""
    if not APP_PASSWORD:
        raise HTTPException(400, "Bu serverdə hesab rejimi qoşulmayıb (APP_PASSWORD təyin olunmayıb).")
    given = body.invite.strip().encode("utf-8", "ignore")
    if not secrets.compare_digest(given, APP_PASSWORD.strip().encode("utf-8")):
        raise HTTPException(403, "Qeydiyyat kodu səhvdir.")
    display = body.username.strip()
    if not USERNAME_RE.match(display):
        raise HTTPException(400, "İstifadəçi adı 3-32 simvol olmalıdır: hərf, rəqəm, nöqtə, tire, alt xətt.")
    email = body.email.strip().casefold()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(400, "E-poçt ünvanı düzgün deyil.")
    if not 8 <= len(body.password) <= 128:
        raise HTTPException(400, "Parol ən azı 8, ən çoxu 128 simvol olmalıdır.")
    if db.get_user_by_email(email):
        raise HTTPException(409, "Bu e-poçt artıq istifadə olunur.")
    uid = db.create_user(display.casefold(), display, hash_password(body.password), email)
    if uid is None:
        raise HTTPException(409, "Bu istifadəçi adı artıq var.")
    row = db.get_user(uid)
    if mail_mode() is not None:   # təsdiq linki göndərilir; qeydiyyat bunu gözləmir
        _VERIFY_SENT[uid] = time.time()
        background.add_task(_send_verify_mail, row, f"{_base_url(request)}/?verify={make_verify_token(row)}")
    if needs_verification(row):   # təsdiqdən əvvəl nişan verilmir: istifadəçi linkə basıb sonra daxil olur
        return {"pending": True, "email": email}
    return {"token": make_token(row), "user": _public_user(row)}


@app.post("/api/auth/login")
@limiter.limit(AUTH_RATE_LIMIT)
def login(request: Request, body: LoginIn):
    if not APP_PASSWORD:
        raise HTTPException(400, "Bu serverdə hesab rejimi qoşulmayıb (APP_PASSWORD təyin olunmayıb).")
    row = db.get_user_by_login(body.username)   # istifadəçi adı və ya e-poçt
    ok = check_password(body.password, row["pw_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise HTTPException(401, "İstifadəçi adı və ya parol səhvdir.")
    if needs_verification(row):   # parol düzgün olandan sonra deyilir: hesabın olub-olmaması sızmasın
        return JSONResponse(status_code=403, content={
            "detail": "E-poçtunuz hələ təsdiqlənməyib. Məktubdakı linkə basın (Spam qovluğuna da baxın).",
            "code": "email_unverified"})
    return {"token": make_token(row), "user": _public_user(row)}


_VERIFY_SENT: dict = {}     # istifadəçi nömrəsi -> son təsdiq məktubunun vaxtı
_CHANGE_SENT: dict = {}     # istifadəçi nömrəsi -> son e-poçt dəyişmə məktubunun vaxtı
_RESET_SENT: dict = {}      # istifadəçi nömrəsi -> son bərpa məktubunun vaxtı (e-poçt spamının qarşısı)
RESET_COOLDOWN = 60


def _base_url(request: Request) -> str:
    return APP_URL or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")


def _send_reset_mail(user: dict, link: str) -> None:
    name = user["display"]
    minutes = max(1, RESET_TTL // 60)
    subject = "Parolun bərpası: Azərbaycan OCR"
    text = (f"Salam, {name}!\n\nHesabınızın parolunu yeniləmək üçün bu linkə keçin (link {minutes} dəqiqə etibarlıdır):\n"
            f"{link}\n\nBu sorğunu siz göndərməmisinizsə, məktuba fikir verməyin: parolunuz dəyişməyəcək.")
    html = (f"<p>Salam, {html_escape(name)}!</p>"
            f"<p>Hesabınızın parolunu yeniləmək üçün aşağıdakı düyməyə basın (link {minutes} dəqiqə etibarlıdır).</p>"
            f'<p><a href="{html_escape(link)}" style="display:inline-block;padding:10px 18px;background:#0b5fff;color:#fff;'
            f'text-decoration:none;border-radius:4px">Parolu yenilə</a></p>'
            f"<p>Düymə işləmirsə, bu ünvanı brauzerə yapışdırın:<br>{html_escape(link)}</p>"
            f"<p>Bu sorğunu siz göndərməmisinizsə, məktuba fikir verməyin: parolunuz dəyişməyəcək.</p>")
    try:
        send_mail(user["email"], subject, text, html)
    except Exception as e:   # istifadəçiyə xəta göstərilmir (hesabın varlığı bilinməsin); səbəb yalnız logda
        log.error("Parol bərpası məktubu göndərilmədi: %s", e)


def _send_verify_mail(user: dict, link: str) -> None:
    name = user["display"]
    hours = max(1, VERIFY_TTL // 3600)
    subject = "E-poçtun təsdiqi: Azərbaycan OCR"
    text = (f"Salam, {name}!\n\nE-poçt ünvanınızı təsdiqləmək üçün bu linkə keçin (link {hours} saat etibarlıdır):\n"
            f"{link}\n\nBu hesabı siz açmamısınızsa, məktuba fikir verməyin.")
    html = (f"<p>Salam, {html_escape(name)}!</p>"
            f"<p>E-poçt ünvanınızı təsdiqləmək üçün aşağıdakı düyməyə basın (link {hours} saat etibarlıdır).</p>"
            f'<p><a href="{html_escape(link)}" style="display:inline-block;padding:10px 18px;background:#0b5fff;color:#fff;'
            f'text-decoration:none;border-radius:4px">E-poçtu təsdiqlə</a></p>'
            f"<p>Düymə işləmirsə, bu ünvanı brauzerə yapışdırın:<br>{html_escape(link)}</p>"
            f"<p>Bu hesabı siz açmamısınızsa, məktuba fikir verməyin.</p>")
    try:
        send_mail(user["email"], subject, text, html)
    except Exception as e:
        log.error("Təsdiq məktubu göndərilmədi: %s", e)


@app.post("/api/auth/verify")
@limiter.limit(AUTH_RATE_LIMIT)
def verify_email(request: Request, body: VerifyIn):
    """E-poçtdakı linkin nişanı ilə ünvanı təsdiqləyir (başqa brauzerdə açılsa da işləyir)."""
    if not APP_PASSWORD:
        raise HTTPException(400, "Bu serverdə hesab rejimi qoşulmayıb (APP_PASSWORD təyin olunmayıb).")
    user = read_verify_token(body.token)
    if user:
        db.set_email_verified(user["id"])
        return {"ok": True}
    change = read_email_change_token(body.token)
    if change:   # e-poçt dəyişmə: yeni ünvan yalnız link açılanda hesaba yazılır
        user, new_email = change
        other = db.get_user_by_email(new_email)
        if other and other["id"] != user["id"]:
            raise HTTPException(409, "Bu e-poçt artıq başqa hesabda istifadə olunur.")
        try:
            db.set_email(user["username"], new_email)
        except IntegrityError:
            raise HTTPException(409, "Bu e-poçt artıq başqa hesabda istifadə olunur.")
        db.set_email_verified(user["id"])
        return {"ok": True, "changed": True}
    raise HTTPException(400, "Link etibarsızdır və ya müddəti bitib. Proqramın içindən yenisini istəyin.")


class ResendIn(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/auth/resend-verification")
@limiter.limit(AUTH_RATE_LIMIT)
def resend_verification(request: Request, background: BackgroundTasks, body: Optional[ResendIn] = None):
    """Təsdiq linkini yenidən göndərir. Nişanla (daxil olmuş) və ya istifadəçi adı + parolla (hələ daxil ola bilməyən) işləyir."""
    if not APP_PASSWORD:
        raise HTTPException(403, "Hesab rejimi (APP_PASSWORD) lazımdır.")
    auth = request.headers.get("authorization", "")
    row = None
    if auth.lower().startswith("bearer "):
        u = read_token(auth[7:].strip())
        row = db.get_user(u["id"]) if u else None
        if not row:
            raise HTTPException(401, "Daxil olmaq lazımdır.")
    elif body and body.username and body.password:
        cand = db.get_user_by_login(body.username)
        ok = check_password(body.password, cand["pw_hash"] if cand else _DUMMY_HASH)
        if not cand or not ok:
            raise HTTPException(401, "İstifadəçi adı və ya parol səhvdir.")
        row = cand
    else:
        raise HTTPException(401, "Daxil olmaq lazımdır.")
    if not row.get("email"):
        raise HTTPException(400, "Hesabda e-poçt yoxdur.")
    if row.get("email_verified"):
        return {"ok": True, "verified": True}
    if mail_mode() is None:
        raise HTTPException(503, "Bu serverdə e-poçt xidməti qoşulmayıb.")
    wait = RESET_COOLDOWN - (time.time() - _VERIFY_SENT.get(row["id"], 0))
    if wait > 0:
        raise HTTPException(429, f"Bir az gözləyin: yeni məktub {int(wait) + 1} saniyədən sonra göndərilə bilər.")
    if len(_VERIFY_SENT) > 1000:
        _VERIFY_SENT.clear()
    _VERIFY_SENT[row["id"]] = time.time()
    background.add_task(_send_verify_mail, row, f"{_base_url(request)}/?verify={make_verify_token(row)}")
    return {"ok": True, "verified": False}


@app.post("/api/auth/forgot")
@limiter.limit(AUTH_RATE_LIMIT)
def forgot_password(request: Request, body: ForgotIn, background: BackgroundTasks):
    """Parol bərpa linki göndərir. Cavab hesab olub-olmamasından asılı olmayaraq eynidir."""
    if not APP_PASSWORD:
        raise HTTPException(400, "Bu serverdə hesab rejimi qoşulmayıb (APP_PASSWORD təyin olunmayıb).")
    if mail_mode() is None:
        raise HTTPException(503, "Bu serverdə e-poçt xidməti qoşulmayıb. Parolu administrator dəyişə bilər.")
    row = db.get_user_by_login(body.login)
    if row and row.get("email"):
        now = time.time()
        if len(_RESET_SENT) > 1000:
            _RESET_SENT.clear()
        if now - _RESET_SENT.get(row["id"], 0) >= RESET_COOLDOWN:
            _RESET_SENT[row["id"]] = now
            link = f"{_base_url(request)}/?reset={make_reset_token(row)}"
            background.add_task(_send_reset_mail, row, link)
    return {"ok": True}


@app.post("/api/auth/reset")
@limiter.limit(AUTH_RATE_LIMIT)
def reset_password(request: Request, body: ResetIn):
    """E-poçtdakı linkin nişanı ilə yeni parol qoyur və birbaşa daxil edir."""
    if not APP_PASSWORD:
        raise HTTPException(400, "Bu serverdə hesab rejimi qoşulmayıb (APP_PASSWORD təyin olunmayıb).")
    if not 8 <= len(body.password) <= 128:
        raise HTTPException(400, "Parol ən azı 8, ən çoxu 128 simvol olmalıdır.")
    user = read_reset_token(body.token)
    if not user:
        raise HTTPException(400, "Link etibarsızdır və ya müddəti bitib. Parol bərpasını yenidən istəyin.")
    db.set_password_by_id(user["id"], hash_password(body.password))
    if user.get("email"):
        db.set_email_verified(user["id"])   # linki e-poçt qutusundan alıb: ünvan həqiqətən onundur
    row = db.get_user(user["id"])
    return {"token": make_token(row), "user": _public_user(row)}


@app.get("/api/usage")
def usage(user: dict = Depends(account_user)):
    return quota_info(user) or {"enabled": False}


@app.post("/api/auth/change-email")
@limiter.limit(AUTH_RATE_LIMIT)
def change_email(request: Request, body: ChangeEmailIn, background: BackgroundTasks,
                 user: dict = Depends(account_user)):
    """E-poçtu dəyişmək üçün yeni ünvana link göndərir (parol tələb olunur).
    Ünvan yalnız link açılanda dəyişir: yanlış yazılsa, köhnə ünvan qalır və hesaba giriş itmir."""
    row = db.get_user(user["id"])
    if not row or not check_password(body.password, row["pw_hash"]):
        raise HTTPException(403, "Parol səhvdir.")
    email = body.email.strip().casefold()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(400, "E-poçt ünvanı düzgün deyil.")
    if email == (row.get("email") or ""):
        raise HTTPException(400, "Bu ünvan artıq hesabınızdadır.")
    other = db.get_user_by_email(email)
    if other and other["id"] != row["id"]:
        raise HTTPException(409, "Bu e-poçt artıq istifadə olunur.")
    if mail_mode() is None:
        raise HTTPException(503, "Bu serverdə e-poçt xidməti qoşulmayıb, ünvanı dəyişmək olmur.")
    wait = RESET_COOLDOWN - (time.time() - _CHANGE_SENT.get(row["id"], 0))
    if wait > 0:
        raise HTTPException(429, f"Bir az gözləyin: yeni məktub {int(wait) + 1} saniyədən sonra göndərilə bilər.")
    if len(_CHANGE_SENT) > 1000:
        _CHANGE_SENT.clear()
    _CHANGE_SENT[row["id"]] = time.time()
    link = f"{_base_url(request)}/?verify={make_email_change_token(row, email)}"
    background.add_task(_send_verify_mail, {**row, "email": email}, link)   # məktub YENİ ünvana gedir
    return {"ok": True, "pending_email": email}


@app.post("/api/auth/change-password")
@limiter.limit(AUTH_RATE_LIMIT)
def change_password(request: Request, body: ChangePasswordIn, user: dict = Depends(account_user)):
    """Daxil olmuş istifadəçi parolunu dəyişir. Köhnə nişanlar bağlanır, yeni nişan qaytarılır."""
    row = db.get_user(user["id"])
    if not row or not check_password(body.current, row["pw_hash"]):
        raise HTTPException(403, "Cari parol səhvdir.")   # 401 deyil: frontend 401-i "sessiya bitib" sayıb çıxarar
    if not 8 <= len(body.new) <= 128:
        raise HTTPException(400, "Yeni parol ən azı 8, ən çoxu 128 simvol olmalıdır.")
    if body.new == body.current:
        raise HTTPException(400, "Yeni parol cari paroldan fərqli olmalıdır.")
    db.set_password_by_id(row["id"], hash_password(body.new))
    row = db.get_user(row["id"])
    return {"token": make_token(row), "user": _public_user(row)}


@app.post("/api/auth/delete-account")
@limiter.limit(AUTH_RATE_LIMIT)
def delete_account(request: Request, body: DeleteAccountIn, user: dict = Depends(account_user)):
    """Hesabı, işləri və tarixçəni həmişəlik silir. Parolu yenidən yazmaq tələb olunur."""
    row = db.get_user(user["id"])
    if not row or not check_password(body.password, row["pw_hash"]):
        raise HTTPException(403, "Parol səhvdir.")
    db.delete_user(row["username"])
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


# ---- Tarixçə (cihazlar arası): hər istifadəçinin qeydləri bazada saxlanılır
@app.get("/api/history")
def history_list(user: dict = Depends(account_user)):
    return {"items": db.hist_list(user["id"])}


@app.put("/api/history/{hid}")
def history_put(hid: int, body: HistIn, user: dict = Depends(account_user)):
    if len(body.fixed) + len(body.raw) > HISTORY_MAX_CHARS or len(json.dumps(body.tables)) > HISTORY_MAX_CHARS:
        raise HTTPException(413, "Nəticə serverdə saxlanmaq üçün çox böyükdür.")
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    db.hist_put(user["id"], hid, body.ts, payload, HISTORY_MAX)
    return {"ok": True}


@app.delete("/api/history/{hid}")
def history_delete(hid: int, user: dict = Depends(account_user)):
    db.hist_delete(user["id"], hid)
    return {"ok": True}


@app.delete("/api/history")
def history_clear(user: dict = Depends(account_user)):
    db.hist_clear(user["id"])
    return {"ok": True}


@app.post("/api/ocr")
@limiter.limit(RATE_LIMIT)
def ocr(request: Request, files: List[UploadFile] = File(...), fix: bool = True,
        enhance: bool = False, tables: bool = False, merge: bool = False,
        user: dict = Depends(current_user)):
    check_quota(user)
    items = read_uploads(files)
    results = []
    for name, data in items:
        limit_msg = quota_error(user)
        if limit_msg:
            results.append({"filename": name, "error": f"{name}: {limit_msg}"})
            continue
        try:
            res = process_bytes(name, data, fix=fix, enhance=enhance, tables=tables)
            charge_quota(user, res)
            results.append(res)
        except HTTPException as e:
            results.append({"filename": name, "error": str(e.detail)})
    if merge and len(results) > 1:
        results = [merge_results(results, f"birləşdirilmiş ({len(results)} səhifə)")]
    return {"results": results}


@app.post("/api/jobs", status_code=202)
@limiter.limit(RATE_LIMIT)
def create_job(request: Request, files: List[UploadFile] = File(...), fix: bool = True,
               enhance: bool = False, tables: bool = False, merge: bool = False,
               user: dict = Depends(current_user)):
    """Faylları qəbul edir və dərhal iş nömrəsi qaytarır; emal arxa planda gedir."""
    check_quota(user)
    items = read_uploads(files)
    db.purge_jobs(JOB_TTL)
    if db.active_job_count() >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, "Server məşğuldur, bir az sonra yenidən cəhd edin.")
    job_id = secrets.token_urlsafe(16)
    db.create_job(job_id, user["id"], {"fix": fix, "enhance": enhance, "tables": tables, "merge": merge}, items)
    _executor.submit(_run_job, job_id)
    return {"job_id": job_id, "total": len(items)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    view = db.job_view(job_id)
    if not view or view["user_id"] != user["id"]:
        raise HTTPException(404, "İş tapılmadı (müddəti bitib).")
    view.pop("user_id")
    return view


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, user: dict = Depends(current_user)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "İş tapılmadı.")
    db.update_job(job_id, cancel=True)
    return {"ok": True}


@app.post("/api/searchable-pdf")
@limiter.limit(RATE_LIMIT)
def searchable_pdf(request: Request, files: List[UploadFile] = File(...), enhance: bool = False,
                   user: dict = Depends(current_user)):
    check_quota(user)
    items = read_uploads(files)
    try:
        pdf, font_warning = build_searchable_pdf(items, enhance)
    except HttpResponseError as e:
        raise HTTPException(502, f"Azure xətası: {e.message}")
    if quota_info(user) is not None:
        db.usage_add(user["id"], _month(), estimate_azure_pages(items))
    headers = {"Content-Disposition": 'attachment; filename="axtarisli.pdf"'}
    if font_warning:
        headers["X-Warning"] = "font"
    return Response(content=pdf, media_type="application/pdf", headers=headers)


@app.post("/api/export/docx", dependencies=[Depends(current_user)])
def export_docx(body: DocxIn):
    from docx import Document

    doc = Document()
    for block in body.text.split("\n\n"):
        if block.strip():
            doc.add_paragraph(block.strip("\n"))  # \n sətir qırılması olur
    buf = io.BytesIO()
    doc.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="netice.docx"'},
    )


@app.post("/api/export/xlsx", dependencies=[Depends(current_user)])
def export_xlsx(body: XlsxIn):
    from openpyxl import Workbook

    if not body.tables:
        raise HTTPException(400, "Cədvəl yoxdur.")
    wb = Workbook()
    wb.remove(wb.active)
    for i, grid in enumerate(body.tables, 1):
        ws = wb.create_sheet(f"Cədvəl {i}")
        for row in grid:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="cedveller.xlsx"'},
    )


# Frontend-i təqdim et (bütün API route-lardan SONRA olmalıdır)
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")