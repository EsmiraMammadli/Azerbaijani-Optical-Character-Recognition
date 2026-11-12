"""API testləri. Azure və Gemini çağırılmır.

İşə salmaq (backend qovluğundan):
    python -m pip install -r requirements-dev.txt
    python -m pytest -q
"""
import io
import itertools
import json
import os
import sys
import tempfile
import time

# main.py import olunanda bu dəyişənlər lazımdır; saxta dəyərlər kifayətdir
os.environ.setdefault("AZURE_DI_ENDPOINT", "https://example.cognitiveservices.azure.com/")
os.environ.setdefault("AZURE_DI_KEY", "test-key")
os.environ.setdefault("RATE_LIMIT", "1000/minute")
os.environ.setdefault("AUTH_RATE_LIMIT", "1000/minute")
os.environ.pop("APP_PASSWORD", None)
# testlər həqiqi bazaya toxunmasın: müvəqqəti SQLite faylı
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db").replace("\\", "/")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import main

# main.py .env-i oxuyub APP_PASSWORD-i geri qaytara bilər; testlərdə giriş kodu söndürülür
main.APP_PASSWORD = None  # açıq rejim (hesabsız)

client = TestClient(main.app)


def upload(name, data, content_type="application/octet-stream", **params):
    return client.post("/api/ocr", params=params, files=[("files", (name, io.BytesIO(data), content_type))])


def test_config():
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert {"auth_required", "max_files", "max_mb", "opencv", "docx_pdf"} <= body.keys()


def test_txt_needs_no_ocr():
    r = upload("a.txt", "Azərbaycan dili".encode("utf-8"))
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["method"] == "document-text"
    assert res["text"] == "Azərbaycan dili"


def test_unsupported_format_is_reported_per_file():
    r = upload("virus.exe", b"MZ")
    assert r.status_code == 200
    assert "dəstəklənməyən" in r.json()["results"][0]["error"]


def test_empty_txt_is_error():
    r = upload("empty.txt", b"   ")
    assert "mətn tapılmadı" in r.json()["results"][0]["error"]


def test_too_big_file():
    r = upload("big.txt", b"a" * (main.MAX_SIZE_MB * 1024 * 1024 + 1))
    assert "böyük" in r.json()["results"][0]["error"]


def test_no_files_is_rejected():
    assert client.post("/api/ocr").status_code == 422


def test_merge_text_files():
    files = [("files", ("a.txt", io.BytesIO(b"bir"), "text/plain")),
             ("files", ("b.txt", io.BytesIO(b"iki"), "text/plain"))]
    r = client.post("/api/ocr", params={"merge": "true"}, files=files)
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["text"] == "bir\n\niki"


def test_open_mode_has_no_history_sync():
    assert client.get("/api/history").status_code == 403


_mail_n = itertools.count(1)


def _register(name, password="parol12345", invite="sirr", email=None):
    email = email if email is not None else f"user{next(_mail_n)}@example.com"
    return client.post("/api/auth/register", json={"username": name, "email": email, "password": password, "invite": invite})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_accounts_register_login_and_isolation(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    files = lambda: [("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))]

    # giriş olmadan işləmir
    assert client.post("/api/ocr", files=files()).status_code == 401
    assert client.get("/api/auth/me", headers=_auth("yalan")).status_code == 401

    # qeydiyyat: səhv kod, qısa parol, uğurlu, təkrar ad
    assert _register("Tərənə1", invite="yanlis").status_code == 403
    assert _register("Tərənə1", password="qisa").status_code == 400
    r = _register("Tərənə1")
    assert r.status_code == 200
    token_a = r.json()["token"]
    assert _register("tərənə1").status_code == 409  # böyük/kiçik hərf fərq etmir

    # giriş
    assert client.post("/api/auth/login", json={"username": "Tərənə1", "password": "yanlis"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "yoxdur", "password": "parol12345"}).status_code == 401
    ok = client.post("/api/auth/login", json={"username": "TƏRƏNƏ1", "password": "parol12345"})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "Tərənə1"

    assert client.get("/api/auth/me", headers=_auth(token_a)).json()["user"]["username"] == "Tərənə1"
    assert client.post("/api/ocr", files=files(), headers=_auth(token_a)).status_code == 200

    # başqa istifadəçi: iş və tarixçə ayrıdır
    token_b = _register("ikinci1").json()["token"]
    job = client.post("/api/jobs", params={"fix": "false"}, files=files(), headers=_auth(token_a)).json()["job_id"]
    assert client.get(f"/api/jobs/{job}", headers=_auth(token_b)).status_code == 404
    assert client.delete(f"/api/jobs/{job}", headers=_auth(token_b)).status_code == 404
    assert client.get(f"/api/jobs/{job}", headers=_auth(token_a)).status_code == 200

    rec = {"ts": 1700000000000, "label": "a.txt", "fixed": "mətn", "raw": "metn", "tables": [[["A", "B"]]]}
    assert client.put("/api/history/1700000000001", json=rec, headers=_auth(token_a)).status_code == 200
    got = client.get("/api/history", headers=_auth(token_a)).json()["items"]
    assert len(got) == 1 and got[0]["id"] == 1700000000001 and got[0]["fixed"] == "mətn"
    assert client.get("/api/history", headers=_auth(token_b)).json()["items"] == []
    rec["fixed"] = "yeni mətn"  # eyni nömrə: yenilənir, təkrarlanmır
    client.put("/api/history/1700000000001", json=rec, headers=_auth(token_a))
    got = client.get("/api/history", headers=_auth(token_a)).json()["items"]
    assert len(got) == 1 and got[0]["fixed"] == "yeni mətn"
    client.delete("/api/history/1700000000001", headers=_auth(token_b))  # başqasınınkını silə bilmir
    assert len(client.get("/api/history", headers=_auth(token_a)).json()["items"]) == 1
    client.delete("/api/history/1700000000001", headers=_auth(token_a))
    assert client.get("/api/history", headers=_auth(token_a)).json()["items"] == []


def test_register_needs_valid_unique_email_and_login_by_email(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    assert _register("poctlu1", email="").status_code == 400
    assert _register("poctlu1", email="bu-poct-deyil").status_code == 400
    assert _register("a@b.cd", email="x@example.com").status_code == 400      # istifadəçi adında @ olmaz
    assert _register("poctlu1", email="Poctlu@Example.com").status_code == 200
    assert _register("poctlu2", email="poctlu@example.com").status_code == 409  # eyni e-poçt (böyük/kiçik fərq etmir)
    # eyni sahəyə həm ad, həm e-poçt yazıla bilər
    by_name = client.post("/api/auth/login", json={"username": "poctlu1", "password": "parol12345"})
    by_mail = client.post("/api/auth/login", json={"username": "POCTLU@example.com", "password": "parol12345"})
    assert by_name.status_code == 200 and by_mail.status_code == 200
    assert by_mail.json()["user"]["username"] == "poctlu1"
    assert client.post("/api/auth/login", json={"username": "poctlu@example.com", "password": "yanlis"}).status_code == 401


def test_config_reports_mail(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "BREVO_API_KEY", "")
    monkeypatch.setattr(main, "SMTP_HOST", "")
    monkeypatch.setattr(main, "MAIL_CONSOLE", False)
    assert client.get("/api/config").json()["mail"] is False
    monkeypatch.setattr(main, "BREVO_API_KEY", "k")
    monkeypatch.setattr(main, "MAIL_FROM", "noreply@example.com")
    assert client.get("/api/config").json()["mail"] is True


def _mail_on(monkeypatch):
    """Məktub göndərmə söndürülür, göndərilənlər siyahıya yığılır."""
    sent = []
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "MAIL_CONSOLE", True)
    monkeypatch.setattr(main, "REQUIRE_VERIFIED", False)   # köhnə axın testləri: təsdiq məcburi deyil
    monkeypatch.setattr(main, "send_mail", lambda to, subject, text, html: sent.append((to, subject, text)))
    main._RESET_SENT.clear()
    main._VERIFY_SENT.clear()
    main._CHANGE_SENT.clear()
    return sent


def _token_of(mail):
    return mail[2].split("/?reset=")[1].split()[0]


def test_forgot_without_mail_service_is_503(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "BREVO_API_KEY", "")
    monkeypatch.setattr(main, "SMTP_HOST", "")
    monkeypatch.setattr(main, "MAIL_CONSOLE", False)
    assert client.post("/api/auth/forgot", json={"login": "kimse"}).status_code == 503


def test_forgot_and_reset_password_flow(monkeypatch):
    sent = _mail_on(monkeypatch)
    old_token = _register("unudan1", email="unudan1@example.com").json()["token"]
    sent.clear()   # qeydiyyatın təsdiq məktubu bu testə aid deyil

    # bilinməyən hesab: cavab eynidir, məktub getmir
    assert client.post("/api/auth/forgot", json={"login": "yoxdur@example.com"}).json() == {"ok": True}
    assert sent == []

    # e-poçt və ya istifadəçi adı ilə sorğu; ikinci sorğu gözləmə müddətində məktub göndərmir
    assert client.post("/api/auth/forgot", json={"login": "UNUDAN1@example.com"}).json() == {"ok": True}
    assert len(sent) == 1 and sent[0][0] == "unudan1@example.com"
    assert client.post("/api/auth/forgot", json={"login": "unudan1"}).status_code == 200
    assert len(sent) == 1

    token = _token_of(sent[0])
    assert client.post("/api/auth/reset", json={"token": token, "password": "qisa"}).status_code == 400
    assert client.post("/api/auth/reset", json={"token": "yalan.nisan", "password": "yeni-parol-123"}).status_code == 400
    ok = client.post("/api/auth/reset", json={"token": token, "password": "yeni-parol-123"})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "unudan1"

    # yeni parolla giriş olur, köhnə parol və köhnə nişan işləmir, link təkrar işləmir
    assert client.post("/api/auth/login", json={"username": "unudan1", "password": "yeni-parol-123"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "unudan1", "password": "parol12345"}).status_code == 401
    assert client.get("/api/auth/me", headers=_auth(old_token)).status_code == 401
    assert client.get("/api/auth/me", headers=_auth(ok.json()["token"])).status_code == 200
    assert client.post("/api/auth/reset", json={"token": token, "password": "basqa-parol-123"}).status_code == 400


def test_reset_token_expires_and_login_token_is_not_a_reset_token(monkeypatch):
    sent = _mail_on(monkeypatch)
    reg = _register("vaxtli1", email="vaxtli1@example.com").json()
    sent.clear()
    # giriş nişanı bərpa üçün yaramır
    assert client.post("/api/auth/reset", json={"token": reg["token"], "password": "yeni-parol-123"}).status_code == 400
    monkeypatch.setattr(main, "RESET_TTL", -5)   # dərhal vaxtı keçən link
    client.post("/api/auth/forgot", json={"login": "vaxtli1"})
    assert client.post("/api/auth/reset", json={"token": _token_of(sent[0]), "password": "yeni-parol-123"}).status_code == 400


def test_old_database_gets_email_column(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    eng = create_engine("sqlite:///" + str(tmp_path / "old.db").replace("\\", "/"))
    with eng.begin() as c:   # e-poçt sütunu olmayan köhnə cədvəl
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(64) NOT NULL UNIQUE, "
                       "display VARCHAR(64) NOT NULL, pw_hash TEXT NOT NULL, created FLOAT NOT NULL)"))
        c.execute(text("INSERT INTO users (username, display, pw_hash, created) VALUES ('a','a','x',1),('b','b','x',1)"))
    main.db._migrate(eng)
    main.db._migrate(eng)   # təkrar çağırış zərərsizdir
    cols = {col["name"] for col in inspect(eng).get_columns("users")}
    assert {"email", "email_verified"} <= cols
    with eng.connect() as c:  # e-poçtu olmayan iki köhnə hesab unikal indeksə mane olmur
        assert c.execute(text("SELECT COUNT(*) FROM users WHERE email IS NULL")).scalar() == 2
        assert c.execute(text("SELECT COUNT(*) FROM users WHERE email_verified = 0")).scalar() == 2   # köhnələr təsdiqsiz


def test_password_change_invalidates_old_token(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    token = _register("deyisen1").json()["token"]
    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 200
    main.db.set_password("deyisen1", main.hash_password("tamamilə-yeni-parol"))
    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401


def _verify_token_of(mail):
    return mail[2].split("/?verify=")[1].split()[0]


def test_email_verification_flow(monkeypatch):
    sent = _mail_on(monkeypatch)
    reg = _register("tesdiq1", email="tesdiq1@example.com").json()
    assert reg["user"]["verified"] is False
    assert len(sent) == 1 and sent[0][0] == "tesdiq1@example.com" and "/?verify=" in sent[0][2]
    token = _verify_token_of(sent[0])
    assert client.get("/api/auth/me", headers=_auth(reg["token"])).json()["user"]["verified"] is False

    # səhv və başqa növ nişanlar yaramır (giriş nişanı da)
    assert client.post("/api/auth/verify", json={"token": "yalan.nisan"}).status_code == 400
    assert client.post("/api/auth/verify", json={"token": reg["token"]}).status_code == 400
    # login olmadan (başqa brauzerdə) açılsa da işləyir
    assert client.post("/api/auth/verify", json={"token": token}).json() == {"ok": True}
    assert client.get("/api/auth/me", headers=_auth(reg["token"])).json()["user"]["verified"] is True
    # təsdiqlənmişə təkrar məktub getmir
    assert client.post("/api/auth/resend-verification", headers=_auth(reg["token"])).json()["verified"] is True
    assert len(sent) == 1


def test_resend_verification_cooldown_and_expiry(monkeypatch):
    sent = _mail_on(monkeypatch)
    reg = _register("tesdiq2", email="tesdiq2@example.com").json()
    h = _auth(reg["token"])
    assert client.post("/api/auth/resend-verification", headers=h).status_code == 429   # qeydiyyatdan dərhal sonra
    main._VERIFY_SENT.clear()
    assert client.post("/api/auth/resend-verification", headers=h).status_code == 200
    assert len(sent) == 2
    assert client.post("/api/auth/resend-verification").status_code == 401
    monkeypatch.setattr(main, "VERIFY_TTL", -5)
    main._VERIFY_SENT.clear()
    client.post("/api/auth/resend-verification", headers=h)
    assert client.post("/api/auth/verify", json={"token": _verify_token_of(sent[-1])}).status_code == 400


def test_email_change_invalidates_verify_link(monkeypatch):
    sent = _mail_on(monkeypatch)
    reg = _register("tesdiq3", email="kohne3@example.com").json()
    token = _verify_token_of(sent[0])
    main.db.set_email("tesdiq3", "yeni3@example.com")   # manage_users.py setemail də bunu edir
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 400
    assert client.get("/api/auth/me", headers=_auth(reg["token"])).json()["user"]["verified"] is False


def test_reset_link_also_verifies_email(monkeypatch):
    sent = _mail_on(monkeypatch)
    reg = _register("tesdiq4", email="tesdiq4@example.com").json()
    sent.clear()
    client.post("/api/auth/forgot", json={"login": "tesdiq4"})
    ok = client.post("/api/auth/reset", json={"token": _token_of(sent[0]), "password": "yeni-parol-123"})
    assert ok.json()["user"]["verified"] is True


def test_change_password_in_app(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    old = _register("deyis2").json()["token"]
    h = _auth(old)
    body = {"current": "parol12345", "new": "tamamilə-yeni-123"}
    assert client.post("/api/auth/change-password", json=body).status_code == 401                          # giriş yoxdur
    assert client.post("/api/auth/change-password", json={**body, "current": "yanlis"}, headers=h).status_code == 403
    assert client.post("/api/auth/change-password", json={**body, "new": "qisa"}, headers=h).status_code == 400
    assert client.post("/api/auth/change-password", json={"current": "parol12345", "new": "parol12345"}, headers=h).status_code == 400
    ok = client.post("/api/auth/change-password", json=body, headers=h)
    assert ok.status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401                                          # köhnə nişan bağlandı
    assert client.get("/api/auth/me", headers=_auth(ok.json()["token"])).status_code == 200
    assert client.post("/api/auth/login", json={"username": "deyis2", "password": "tamamilə-yeni-123"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "deyis2", "password": "parol12345"}).status_code == 401


def test_delete_account_removes_everything(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    a = _register("silinen1", email="silinen1@example.com").json()["token"]
    b = _register("qalan1", email="qalan1@example.com").json()["token"]
    rec = {"ts": 1700000000000, "label": "a.txt", "fixed": "mətn", "raw": "metn"}
    client.put("/api/history/1700000000009", json=rec, headers=_auth(a))
    client.put("/api/history/1700000000009", json=rec, headers=_auth(b))
    assert client.post("/api/auth/delete-account", json={"password": "parol12345"}).status_code == 401
    assert client.post("/api/auth/delete-account", json={"password": "yanlis"}, headers=_auth(a)).status_code == 403
    assert client.get("/api/auth/me", headers=_auth(a)).status_code == 200      # səhv parolla silinmədi
    assert client.post("/api/auth/delete-account", json={"password": "parol12345"}, headers=_auth(a)).json() == {"ok": True}
    assert client.get("/api/auth/me", headers=_auth(a)).status_code == 401
    assert client.post("/api/auth/login", json={"username": "silinen1", "password": "parol12345"}).status_code == 401
    assert main.db.get_user_by_email("silinen1@example.com") is None
    assert len(client.get("/api/history", headers=_auth(b)).json()["items"]) == 1   # başqasına toxunulmayıb
    # e-poçt və ad yenidən istifadə oluna bilər
    assert _register("silinen1", email="silinen1@example.com").status_code == 200


def test_healthz_is_light_and_open(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")   # giriş tələb olunan rejimdə də açıqdır
    assert client.get("/healthz").json() == {"ok": True}
    assert client.head("/healthz").status_code == 200


def _txt(name="a.txt", data=b"x"):
    return [("files", (name, io.BytesIO(data), "text/plain"))]


def _fake_azure(monkeypatch, pages=2):
    """Azure çağırılmır: sənəd 'pages' səhifəlik OCR nəticəsi kimi qaytarılır."""
    def fake(filename, data, fix=True, enhance=False, tables=False):
        return {"filename": filename, "method": "azure-ocr", "pages": pages, "text": "mətn", "raw_text": "mətn",
                "tables": [], "warnings": [], "avg_confidence": 0.9, "llm_fixed": False}
    monkeypatch.setattr(main, "process_bytes", fake)


def test_monthly_quota(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "USER_MONTHLY_PAGES", 3)
    _fake_azure(monkeypatch, pages=2)
    a = _register("kvota1").json()["token"]
    b = _register("kvota2").json()["token"]
    ha = _auth(a)
    assert client.get("/api/usage", headers=ha).json()["left"] == 3
    r = client.post("/api/ocr", files=_txt("1.png"), headers=ha)
    assert r.status_code == 200 and "error" not in r.json()["results"][0]
    assert client.get("/api/usage", headers=ha).json() == {"enabled": True, "limit": 3, "used": 2, "left": 1, "month": main._month()}
    # 1 səhifə qalıb: bu sorğu keçir (ən çox bir fayl qədər aşmaq olar), sonrakı bloklanır
    assert client.post("/api/ocr", files=_txt("2.png"), headers=ha).status_code == 200
    r = client.post("/api/ocr", files=_txt("3.png"), headers=ha)
    assert r.status_code == 429 and "Aylıq limit" in r.json()["detail"]
    assert client.post("/api/jobs", files=_txt("4.png"), headers=ha).status_code == 429
    # başqa istifadəçiyə təsir etmir
    assert client.post("/api/ocr", files=_txt("5.png"), headers=_auth(b)).status_code == 200
    # çox fayllı sorğuda limit fayllar arasında da yoxlanır
    c = _register("kvota3").json()["token"]
    two = [("files", ("a.png", io.BytesIO(b"1"), "text/plain")), ("files", ("b.png", io.BytesIO(b"2"), "text/plain")),
           ("files", ("c.png", io.BytesIO(b"3"), "text/plain"))]
    res = client.post("/api/ocr", files=two, headers=_auth(c)).json()["results"]
    assert ["error" in x for x in res] == [False, False, True]


def test_quota_ignores_files_without_azure_and_can_be_off(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "USER_MONTHLY_PAGES", 1)
    t = _register("kvota4").json()["token"]
    for _ in range(3):   # txt Azure-a getmir: limitə girmir
        assert client.post("/api/ocr", files=_txt(), headers=_auth(t)).status_code == 200
    assert client.get("/api/usage", headers=_auth(t)).json()["used"] == 0
    monkeypatch.setattr(main, "USER_MONTHLY_PAGES", 0)   # söndürülüb
    assert client.get("/api/usage", headers=_auth(t)).json() == {"enabled": False}


def test_quota_stops_running_job(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "USER_MONTHLY_PAGES", 2)
    _fake_azure(monkeypatch, pages=2)
    h = _auth(_register("kvota5").json()["token"])
    files = [("files", ("a.png", io.BytesIO(b"1"), "text/plain")), ("files", ("b.png", io.BytesIO(b"2"), "text/plain"))]
    jid = client.post("/api/jobs", files=files, headers=h).json()["job_id"]
    for _ in range(50):   # iş arxa planda gedir
        job = client.get(f"/api/jobs/{jid}", headers=h).json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    errs = [r.get("error") for r in job["results"]]
    assert errs[0] is None and "Aylıq limit" in errs[1]


def _login_verified(name, email, sent):
    """Hesab açır, e-poçtu təsdiqləyir (məktub xidməti qoşulu, təsdiq tələb olunur) və giriş nişanını qaytarır."""
    reg = _register(name, email=email)
    assert reg.status_code == 200
    mail = [m for m in sent if m[0] == email][-1]
    assert client.post("/api/auth/verify", json={"token": _verify_token_of(mail)}).status_code == 200
    r = client.post("/api/auth/login", json={"username": name, "password": "parol12345"})
    assert r.status_code == 200
    return r.json()["token"]


def test_change_email_needs_confirmation_from_new_address(monkeypatch):
    sent = _mail_on(monkeypatch)
    token = _login_verified("poct7", "kohne7@example.com", sent)
    h = _auth(token)
    _login_verified("poct8", "basqa8@example.com", sent)
    sent.clear()
    body = {"password": "parol12345", "email": "Yeni7@Example.com"}
    assert client.post("/api/auth/change-email", json=body).status_code == 401
    assert client.post("/api/auth/change-email", json={**body, "password": "yanlis"}, headers=h).status_code == 403
    assert client.post("/api/auth/change-email", json={**body, "email": "pis-poct"}, headers=h).status_code == 400
    assert client.post("/api/auth/change-email", json={**body, "email": "kohne7@example.com"}, headers=h).status_code == 400
    assert client.post("/api/auth/change-email", json={**body, "email": "basqa8@example.com"}, headers=h).status_code == 409
    ok = client.post("/api/auth/change-email", json=body, headers=h)
    assert ok.status_code == 200 and ok.json()["pending_email"] == "yeni7@example.com"
    assert len(sent) == 1 and sent[0][0] == "yeni7@example.com" and "/?verify=" in sent[0][2]   # link YENİ ünvana gedir
    # link açılana qədər köhnə ünvan qalır və giriş işləyir (yanlış yazılsa kilidlənmə olmur)
    assert client.get("/api/auth/me", headers=h).json()["user"]["email"] == "kohne7@example.com"
    assert client.post("/api/auth/login", json={"username": "kohne7@example.com", "password": "parol12345"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "yeni7@example.com", "password": "parol12345"}).status_code == 401
    link_token = _verify_token_of(sent[0])
    assert client.post("/api/auth/verify", json={"token": link_token}).json() == {"ok": True, "changed": True}
    assert client.post("/api/auth/login", json={"username": "yeni7@example.com", "password": "parol12345"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "kohne7@example.com", "password": "parol12345"}).status_code == 401
    assert client.get("/api/auth/me", headers=h).json()["user"] == {**client.get("/api/auth/me", headers=h).json()["user"], "verified": True, "email": "yeni7@example.com"}


def test_change_email_link_dies_on_password_change_or_taken_address(monkeypatch):
    sent = _mail_on(monkeypatch)
    h = _auth(_login_verified("poct9", "kohne9@example.com", sent))
    sent.clear()
    client.post("/api/auth/change-email", json={"password": "parol12345", "email": "yeni9@example.com"}, headers=h)
    link = _verify_token_of(sent[0])
    # parol dəyişdi (məs. hesab ələ keçirilmişdi): köhnə dəyişmə linki işləmir
    main.db.set_password("poct9", main.hash_password("tamamilə-yeni-parol"))
    assert client.post("/api/auth/verify", json={"token": link}).status_code == 400
    # link açılmazdan əvvəl ünvanı başqası tutdu
    sent.clear()
    main._CHANGE_SENT.clear()
    h = _auth(client.post("/api/auth/login", json={"username": "poct9", "password": "tamamilə-yeni-parol"}).json()["token"])
    client.post("/api/auth/change-email", json={"password": "tamamilə-yeni-parol", "email": "tutulan9@example.com"}, headers=h)
    link = _verify_token_of(sent[0])
    main.db.create_user("tutan9", "Tutan9", main.hash_password("parol12345"), "tutulan9@example.com")
    assert client.post("/api/auth/verify", json={"token": link}).status_code == 409


def test_backup_and_restore_roundtrip():
    import backup_db
    uid = main.db.create_user("yedek1", "Yedek1", main.hash_password("parol12345"), "yedek1@example.com")
    main.db.hist_put(uid, 7, 1700000000000, {"label": "a", "fixed": "mətn"}, 100)
    main.db.usage_add(uid, "2026-11", 5)
    data = backup_db.make_backup()
    assert any(u["id"] == uid for u in data["tables"]["users"])
    data = json.loads(json.dumps(data))   # fayla yazılıb oxunmağı simulyasiya
    other = main.db.create_user("yedek2", "Yedek2", main.hash_password("parol12345"), "yedek2@example.com")
    assert backup_db.restore_backup(data) == {"users": 0, "history": 0, "usage": 0}   # hamısı artıq var: toxunmur
    main.db.delete_user("yedek1")
    assert main.db.get_user(uid) is None and main.db.usage_get(uid, "2026-11") == 0
    added = backup_db.restore_backup(data)
    assert added["users"] == 1 and added["usage"] == 1
    back = main.db.get_user(uid)
    assert back["username"] == "yedek1" and back["email"] == "yedek1@example.com"
    assert main.db.hist_list(uid)[0]["fixed"] == "mətn" and main.db.usage_get(uid, "2026-11") == 5
    assert main.db.get_user(other)["username"] == "yedek2"   # mövcud hesablara toxunulmayıb


def test_unfinished_job_resumes_after_restart():
    """Server yenidən başlayanda növbədə qalan iş bərpa olunur və bitir."""
    main.db.create_job("resume-test", 0, {"fix": False, "enhance": False, "tables": False, "merge": False},
                       [("a.txt", b"bir"), ("b.txt", b"iki")])
    # birinci fayl 'running' vəziyyətində qalıb (proses ortada ölüb kimi)
    main.db.claim_next_file("resume-test")
    main.recover_jobs()
    job = _wait_job("resume-test")
    assert job["status"] == "done" and job["done"] == 2
    assert [r["text"] for r in job["results"]] == ["bir", "iki"]


@pytest.mark.skipif(main.find_soffice() is None, reason="LibreOffice quraşdırılmayıb")
def test_searchable_pdf_from_docx():
    import pymupdf
    from docx import Document

    doc = Document()
    doc.add_paragraph("Azərbaycan dili: Məmməd və dəstək.")
    buf = io.BytesIO()
    doc.save(buf)
    r = client.post("/api/searchable-pdf", files=[("files", ("a.docx", io.BytesIO(buf.getvalue()), "application/octet-stream"))])
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    text = "".join(page.get_text() for page in pymupdf.open(stream=r.content, filetype="pdf"))
    # şrift və pymupdf versiyasından asılı olaraq hərflər ayrı sətirlərə düşə bilər: ə-lərin hamısının olması vacibdir
    assert text.count("ə") >= 6


def test_docx_pdf_without_libreoffice_gives_clear_error(monkeypatch):
    monkeypatch.setattr(main, "find_soffice", lambda: None)
    r = client.post("/api/searchable-pdf", files=[("files", ("a.docx", io.BytesIO(b"PK"), "application/octet-stream"))])
    assert r.status_code == 503 and "LibreOffice" in r.json()["detail"]


def test_export_docx():
    r = client.post("/api/export/docx", json={"text": "Salam\n\nDünya"})
    assert r.status_code == 200
    assert r.content[:2] == b"PK"  # docx = zip


def test_export_xlsx():
    r = client.post("/api/export/xlsx", json={"tables": [[["A", "B"], ["1", "2"]]]})
    assert r.status_code == 200
    assert r.content[:2] == b"PK"


def test_export_xlsx_without_tables():
    assert client.post("/api/export/xlsx", json={"tables": []}).status_code == 400


def test_normalize_az():
    assert main.normalize_az("Mǝmmǝd") == "Məmməd"
    assert main.normalize_az("Ǝzǝrbaycan") == "Əzərbaycan"


def test_html_reader_strips_tags_and_scripts():
    html = b"<html><script>x=1</script><body><p>Salam</p><p>D\xc3\xbcnya</p></body></html>"
    text = main.read_html(html)
    assert "Salam" in text and "Dünya" in text and "x=1" not in text


def test_xlsx_and_csv_tables_extracted():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Ad", "Say"])
    ws.append(["Test", 5.0])
    buf = io.BytesIO()
    wb.save(buf)
    res = upload("c.xlsx", buf.getvalue(), fix="false").json()["results"][0]
    assert res["tables"] == [[["Ad", "Say"], ["Test", "5"]]]

    res = upload("c.csv", "a;b\n1;2\n".encode(), fix="false").json()["results"][0]
    assert res["tables"] == [[["a", "b"], ["1", "2"]]]


def _wait_job(job_id, tries=50):
    import time
    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "cancelled", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("iş vaxtında bitmədi")


def test_job_runs_in_background_and_returns_results():
    files = [("files", ("a.txt", io.BytesIO(b"bir"), "text/plain")),
             ("files", ("b.txt", io.BytesIO(b"iki"), "text/plain")),
             ("files", ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream"))]
    r = client.post("/api/jobs", params={"fix": "false"}, files=files)
    assert r.status_code == 202
    body = r.json()
    assert body["total"] == 3
    job = _wait_job(body["job_id"])
    assert job["status"] == "done" and job["done"] == 3
    assert [f["status"] for f in job["files"]] == ["ok", "ok", "err"]
    assert job["results"][0]["text"] == "bir"
    assert "error" in job["results"][2]


def test_job_merge_and_unknown_job():
    files = [("files", ("a.txt", io.BytesIO(b"bir"), "text/plain")),
             ("files", ("b.txt", io.BytesIO(b"iki"), "text/plain"))]
    r = client.post("/api/jobs", params={"fix": "false", "merge": "true"}, files=files)
    job = _wait_job(r.json()["job_id"])
    assert len(job["results"]) == 1 and "bir" in job["results"][0]["text"]
    assert client.get("/api/jobs/yoxdur").status_code == 404
    assert client.delete("/api/jobs/yoxdur").status_code == 404

def test_verification_required_before_login(monkeypatch):
    sent = _mail_on(monkeypatch)
    monkeypatch.setattr(main, "REQUIRE_VERIFIED", True)

    # qeydiyyat nişan vermir, yalnız təsdiq məktubu göndərir
    r = _register("mecburi1", email="mecburi1@example.com")
    assert r.status_code == 200 and r.json() == {"pending": True, "email": "mecburi1@example.com"}
    assert len(sent) == 1 and "/?verify=" in sent[0][2]

    # səhv parolla 401 (təsdiq vəziyyəti sızmır), düzgün parolla 403 + kod
    assert client.post("/api/auth/login", json={"username": "mecburi1", "password": "yanlis-parol"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "mecburi1", "password": "parol12345"})
    assert blocked.status_code == 403 and blocked.json()["code"] == "email_unverified"

    # yenidən göndərmə: səhv parol 401, düzgün parol + gözləmə müddəti 429, sonra 200
    assert client.post("/api/auth/resend-verification", json={"username": "mecburi1", "password": "yanlis"}).status_code == 401
    assert client.post("/api/auth/resend-verification", json={"username": "mecburi1", "password": "parol12345"}).status_code == 429
    main._VERIFY_SENT.clear()
    assert client.post("/api/auth/resend-verification", json={"username": "mecburi1", "password": "parol12345"}).json() == {"ok": True, "verified": False}
    assert len(sent) == 2

    # linkə basandan sonra giriş açılır
    assert client.post("/api/auth/verify", json={"token": _verify_token_of(sent[-1])}).json() == {"ok": True}
    ok = client.post("/api/auth/login", json={"username": "mecburi1", "password": "parol12345"})
    assert ok.status_code == 200 and ok.json()["user"]["verified"] is True
    assert client.get("/api/auth/me", headers=_auth(ok.json()["token"])).status_code == 200


def test_unverified_existing_token_is_rejected_when_required(monkeypatch):
    _mail_on(monkeypatch)
    reg = _register("kohnenisan1", email="kohnenisan1@example.com").json()   # enforcement söndürülüb: nişan verilir
    assert client.get("/api/auth/me", headers=_auth(reg["token"])).status_code == 200
    monkeypatch.setattr(main, "REQUIRE_VERIFIED", True)
    assert client.get("/api/auth/me", headers=_auth(reg["token"])).status_code == 401
    assert client.post("/api/ocr", headers=_auth(reg["token"]), files=[("files", ("a.txt", b"salam", "text/plain"))]).status_code == 401


def test_verification_not_required_without_mail_service_or_email(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "sirr")
    monkeypatch.setattr(main, "BREVO_API_KEY", "")
    monkeypatch.setattr(main, "SMTP_HOST", "")
    monkeypatch.setattr(main, "MAIL_CONSOLE", False)
    monkeypatch.setattr(main, "REQUIRE_VERIFIED", True)
    r = _register("mailsiz1", email="mailsiz1@example.com")
    assert r.status_code == 200 and "token" in r.json()     # məktub xidməti yoxdur: gözləmək mənasızdır
    assert client.get("/api/config").json()["verify_required"] is False
    # məktub xidməti var, amma e-poçtu olmayan köhnə hesab bloklanmır
    monkeypatch.setattr(main, "MAIL_CONSOLE", True)
    main.db.create_user("kohnehesab1", "kohnehesab1", main.hash_password("parol12345"), "")
    assert client.post("/api/auth/login", json={"username": "kohnehesab1", "password": "parol12345"}).status_code == 200
    assert client.get("/api/config").json()["verify_required"] is True


def test_reset_link_verifies_and_logs_in_when_required(monkeypatch):
    sent = _mail_on(monkeypatch)
    monkeypatch.setattr(main, "REQUIRE_VERIFIED", True)
    _register("bərpa-tesdiq1".replace("ə", "e"), email="berpatesdiq1@example.com")
    sent.clear()
    client.post("/api/auth/forgot", json={"login": "berpa-tesdiq1"})
    r = client.post("/api/auth/reset", json={"token": _token_of(sent[0]), "password": "yeni-parol-123"})
    assert r.status_code == 200 and "token" in r.json()   # e-poçt qutusuna çıxışı sübut etdi: təsdiqlənir
    assert client.post("/api/auth/login", json={"username": "berpa-tesdiq1", "password": "yeni-parol-123"}).status_code == 200
