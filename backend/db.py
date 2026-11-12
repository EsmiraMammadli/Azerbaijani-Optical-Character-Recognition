"""Verilənlər bazası: istifadəçilər, iş növbəsi və tarixçə.

Lokalda SQLite faylı işlədilir (backend/data/az_ocr.db).
Serverdə DATABASE_URL mühit dəyişəni verilsə (məsələn Neon Postgres), o işlədilir.
Render-in pulsuz planında disk hər yenidən başlamada silinir, ona görə server üçün xarici baza lazımdır.
"""
import json
import os
import time

from sqlalchemy import (
    BigInteger, Boolean, Column, Float, Integer, LargeBinary, MetaData, String, Table, Text,
    create_engine, delete, false, func, insert, inspect, select, text, update,
)
from sqlalchemy.exc import IntegrityError


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(folder, exist_ok=True)
        return "sqlite:///" + os.path.join(folder, "az_ocr.db").replace("\\", "/")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DB_URL = _db_url()
IS_SQLITE = DB_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False, "timeout": 30})
else:
    # Neon boş qalanda bağlantını kəsir: pool_pre_ping ölü bağlantını avtomatik yeniləyir
    engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=240, pool_size=5, max_overflow=5)

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(64), unique=True, nullable=False),   # kiçik hərflə (açar)
    Column("display", String(64), nullable=False),                 # yazıldığı kimi
    Column("email", String(254)),                                  # kiçik hərflə; unikal indeks _migrate-də yaradılır
    Column("email_verified", Boolean, nullable=False, default=False, server_default=false()),
    Column("pw_hash", Text, nullable=False),
    Column("created", Float, nullable=False),
)

jobs = Table(
    "jobs", metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", Integer, index=True, nullable=False),
    Column("status", String(16), nullable=False),     # queued, running, done, cancelled, failed
    Column("fix", Boolean, nullable=False),
    Column("enhance", Boolean, nullable=False),
    Column("tables", Boolean, nullable=False),
    Column("merge", Boolean, nullable=False),
    Column("cancel", Boolean, nullable=False, default=False),
    Column("total", Integer, nullable=False),
    Column("done", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("created", Float, nullable=False),
    Column("updated", Float, nullable=False),
    Column("results", Text),
)

job_files = Table(
    "job_files", metadata,
    Column("job_id", String(64), primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("status", String(16), nullable=False),     # queued, running, ok, err
    Column("error", Text),
    Column("method", String(64)),
    Column("pages", Integer),
    Column("confidence", Float),
    Column("llm", Boolean, default=False),
    Column("data", LargeBinary),                      # emaldan sonra silinir
    Column("result", Text),
)

history = Table(
    "history", metadata,
    Column("user_id", Integer, primary_key=True),
    Column("hid", BigInteger, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("payload", Text, nullable=False),
)


usage = Table(
    "usage", metadata,
    Column("user_id", Integer, primary_key=True),
    Column("month", String(7), primary_key=True),      # "2026-11" (UTC)
    Column("pages", Integer, nullable=False, default=0),
)


def _migrate(eng):
    """Köhnə bazaya yeni sütunları (email, email_verified) əlavə edir. Təkrar çağırmaq zərərsizdir."""
    with eng.begin() as c:
        cols = {col["name"] for col in inspect(c).get_columns("users")}
        if "email" not in cols:
            c.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(254)"))
        # NULL dəyərlər təkrar ola bilər (e-poçtu olmayan köhnə hesablar), dolu dəyərlər unikaldır
        if "email_verified" not in cols:
            c.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT "
                           + ("0" if eng.dialect.name == "sqlite" else "FALSE")))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users (email)"))


def init_db():
    metadata.create_all(engine)
    _migrate(engine)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- istifadəçilər
def create_user(username: str, display: str, pw_hash: str, email: str = None):
    """Yeni istifadəçinin nömrəsini qaytarır; ad (və ya e-poçt) məşğuldursa None."""
    try:
        with engine.begin() as c:
            res = c.execute(insert(users).values(
                username=username, display=display, pw_hash=pw_hash,
                email=(email or "").strip().casefold() or None, created=time.time()))
            return int(res.inserted_primary_key[0])
    except IntegrityError:
        return None


def get_user_by_name(username: str):
    with engine.connect() as c:
        row = c.execute(select(users).where(users.c.username == username)).mappings().first()
    return dict(row) if row else None


def get_user_by_email(email: str):
    email = (email or "").strip().casefold()
    if not email:
        return None
    with engine.connect() as c:
        row = c.execute(select(users).where(users.c.email == email)).mappings().first()
    return dict(row) if row else None


def get_user_by_login(ident: str):
    """Giriş sahəsinə yazılan dəyər: e-poçt (@ var) və ya istifadəçi adı."""
    ident = (ident or "").strip().casefold()
    if "@" in ident:
        user = get_user_by_email(ident)
        if user:
            return user
    return get_user_by_name(ident)


def get_user(user_id: int):
    with engine.connect() as c:
        row = c.execute(select(users).where(users.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def list_users():
    with engine.connect() as c:
        rows = c.execute(select(users).order_by(users.c.id)).mappings().all()
    return [dict(r) for r in rows]


def set_password(username: str, pw_hash: str) -> bool:
    with engine.begin() as c:
        res = c.execute(update(users).where(users.c.username == username).values(pw_hash=pw_hash))
        return res.rowcount > 0


def set_password_by_id(user_id: int, pw_hash: str) -> bool:
    with engine.begin() as c:
        res = c.execute(update(users).where(users.c.id == user_id).values(pw_hash=pw_hash))
        return res.rowcount > 0


def set_email(username: str, email: str) -> bool:
    """Mövcud hesaba e-poçt yazır (təsdiq sıfırlanır); başqasında varsa IntegrityError qaldırır."""
    with engine.begin() as c:
        res = c.execute(update(users).where(users.c.username == username)
                        .values(email=(email or "").strip().casefold() or None, email_verified=False))
        return res.rowcount > 0


def set_email_verified(user_id: int) -> bool:
    with engine.begin() as c:
        res = c.execute(update(users).where(users.c.id == user_id).values(email_verified=True))
        return res.rowcount > 0


def delete_user(username: str) -> bool:
    user = get_user_by_name(username)
    if not user:
        return False
    with engine.begin() as c:
        c.execute(delete(history).where(history.c.user_id == user["id"]))
        c.execute(delete(usage).where(usage.c.user_id == user["id"]))
        old = [r[0] for r in c.execute(select(jobs.c.id).where(jobs.c.user_id == user["id"]))]
        for jid in old:
            c.execute(delete(job_files).where(job_files.c.job_id == jid))
        c.execute(delete(jobs).where(jobs.c.user_id == user["id"]))
        c.execute(delete(users).where(users.c.id == user["id"]))
    return True


# ---------------------------------------------------------------- aylıq istifadə (Azure səhifələri)
def usage_get(user_id: int, month: str) -> int:
    with engine.connect() as c:
        val = c.execute(select(usage.c.pages).where(usage.c.user_id == user_id, usage.c.month == month)).scalar()
    return int(val or 0)


def usage_add(user_id: int, month: str, pages: int):
    if pages <= 0:
        return
    cond = (usage.c.user_id == user_id, usage.c.month == month)
    with engine.begin() as c:
        n = c.execute(update(usage).where(*cond).values(pages=usage.c.pages + pages)).rowcount
    if n == 0:
        try:
            with engine.begin() as c:
                c.execute(insert(usage).values(user_id=user_id, month=month, pages=pages))
        except IntegrityError:  # eyni anda iki sorğu gəldi: üstünə əlavə et
            with engine.begin() as c:
                c.execute(update(usage).where(*cond).values(pages=usage.c.pages + pages))


def usage_list(month: str):
    with engine.connect() as c:
        rows = c.execute(select(usage.c.user_id, usage.c.pages).where(usage.c.month == month)).all()
    return {int(u): int(p) for u, p in rows}


# ---------------------------------------------------------------- iş növbəsi
def create_job(job_id: str, user_id: int, opts: dict, items: list):
    now = time.time()
    with engine.begin() as c:
        c.execute(insert(jobs).values(
            id=job_id, user_id=user_id, status="queued",
            fix=bool(opts["fix"]), enhance=bool(opts["enhance"]),
            tables=bool(opts["tables"]), merge=bool(opts["merge"]),
            cancel=False, total=len(items), done=0, created=now, updated=now))
        for i, (name, data) in enumerate(items):
            c.execute(insert(job_files).values(
                job_id=job_id, idx=i, name=name, status="queued", data=data, llm=False))


def get_job(job_id: str):
    with engine.connect() as c:
        row = c.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
    return dict(row) if row else None


def job_view(job_id: str):
    """İşin görünüşü (frontend-in oxuduğu format). Tapılmasa None."""
    job = get_job(job_id)
    if not job:
        return None
    with engine.connect() as c:
        rows = c.execute(
            select(job_files.c.idx, job_files.c.name, job_files.c.status, job_files.c.error,
                   job_files.c.method, job_files.c.pages, job_files.c.confidence, job_files.c.llm)
            .where(job_files.c.job_id == job_id).order_by(job_files.c.idx)
        ).mappings().all()
    files = [{"name": r["name"], "status": r["status"], "error": r["error"], "method": r["method"],
              "pages": r["pages"], "confidence": r["confidence"], "llm": bool(r["llm"])} for r in rows]
    return {
        "id": job["id"], "user_id": job["user_id"], "status": job["status"],
        "done": job["done"], "total": job["total"], "error": job["error"], "files": files,
        "results": json.loads(job["results"]) if job["status"] == "done" and job["results"] else None,
    }


def update_job(job_id: str, **values):
    values["updated"] = time.time()
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id == job_id).values(**values))


def job_cancelled(job_id: str) -> bool:
    with engine.connect() as c:
        val = c.execute(select(jobs.c.cancel).where(jobs.c.id == job_id)).scalar()
    return bool(val)


def active_job_count() -> int:
    with engine.connect() as c:
        return int(c.execute(
            select(func.count()).select_from(jobs).where(jobs.c.status.in_(("queued", "running")))
        ).scalar() or 0)


def claim_next_file(job_id: str):
    """Növbədəki ilk faylı 'running' edib (idx, ad, bayt) qaytarır; qalmayıbsa None."""
    with engine.begin() as c:
        row = c.execute(
            select(job_files.c.idx, job_files.c.name, job_files.c.data)
            .where(job_files.c.job_id == job_id, job_files.c.status.in_(("queued", "running")))
            .order_by(job_files.c.idx).limit(1)
        ).first()
        if not row:
            return None
        c.execute(update(job_files).where(job_files.c.job_id == job_id, job_files.c.idx == row[0])
                  .values(status="running"))
        return row[0], row[1], bytes(row[2] or b"")


def finish_file(job_id: str, idx: int, res: dict):
    err = res.get("error")
    with engine.begin() as c:
        c.execute(update(job_files).where(job_files.c.job_id == job_id, job_files.c.idx == idx).values(
            status="err" if err else "ok", error=err, method=res.get("method"),
            pages=res.get("pages"), confidence=res.get("avg_confidence"),
            llm=bool(res.get("llm_fixed")), data=None, result=_dumps(res)))
        done = c.execute(
            select(func.count()).select_from(job_files)
            .where(job_files.c.job_id == job_id, job_files.c.status.in_(("ok", "err")))
        ).scalar()
        c.execute(update(jobs).where(jobs.c.id == job_id).values(done=int(done), updated=time.time()))


def job_results_list(job_id: str):
    with engine.connect() as c:
        rows = c.execute(
            select(job_files.c.result).where(job_files.c.job_id == job_id).order_by(job_files.c.idx)
        ).all()
    return [json.loads(r[0]) for r in rows if r[0]]


def complete_job(job_id: str, results: list):
    update_job(job_id, status="done", results=_dumps(results))


def cancel_job_final(job_id: str):
    with engine.begin() as c:
        c.execute(update(job_files).where(job_files.c.job_id == job_id).values(data=None))
        c.execute(update(jobs).where(jobs.c.id == job_id).values(status="cancelled", updated=time.time()))


def unfinished_job_ids():
    """Server yenidən başlayanda yarımçıq qalan işləri növbəyə qaytarır."""
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(
            select(jobs.c.id).where(jobs.c.status.in_(("queued", "running"))).order_by(jobs.c.created))]
        for jid in ids:
            c.execute(update(job_files).where(job_files.c.job_id == jid, job_files.c.status == "running")
                      .values(status="queued"))
    return ids


def purge_jobs(ttl_seconds: int):
    limit = time.time() - ttl_seconds
    with engine.begin() as c:
        old = [r[0] for r in c.execute(
            select(jobs.c.id).where(jobs.c.status.in_(("done", "cancelled", "failed")), jobs.c.updated < limit))]
        for jid in old:
            c.execute(delete(job_files).where(job_files.c.job_id == jid))
            c.execute(delete(jobs).where(jobs.c.id == jid))


# ---------------------------------------------------------------- tarixçə
def hist_list(user_id: int):
    with engine.connect() as c:
        rows = c.execute(
            select(history.c.hid, history.c.payload).where(history.c.user_id == user_id)
            .order_by(history.c.ts.desc())
        ).all()
    out = []
    for hid, payload in rows:
        rec = json.loads(payload)
        rec["id"] = int(hid)
        out.append(rec)
    return out


def hist_put(user_id: int, hid: int, ts: int, payload: dict, keep: int):
    text = _dumps(payload)
    cond = (history.c.user_id == user_id, history.c.hid == hid)
    with engine.begin() as c:
        updated = c.execute(update(history).where(*cond).values(ts=ts, payload=text)).rowcount > 0
    if not updated:
        try:
            with engine.begin() as c:
                c.execute(insert(history).values(user_id=user_id, hid=hid, ts=ts, payload=text))
        except IntegrityError:  # eyni anda iki sorğu gəldi: yenilə
            with engine.begin() as c:
                c.execute(update(history).where(*cond).values(ts=ts, payload=text))
    with engine.begin() as c:  # limiti aşan ən köhnə qeydləri sil
        extra = c.execute(
            select(history.c.hid).where(history.c.user_id == user_id)
            .order_by(history.c.ts.desc()).offset(keep)
        ).all()
        for (old,) in extra:
            c.execute(delete(history).where(history.c.user_id == user_id, history.c.hid == old))


def hist_delete(user_id: int, hid: int):
    with engine.begin() as c:
        c.execute(delete(history).where(history.c.user_id == user_id, history.c.hid == hid))


def hist_clear(user_id: int):
    with engine.begin() as c:
        c.execute(delete(history).where(history.c.user_id == user_id))
