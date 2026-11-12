"""Bazanın ehtiyat nüsxəsi: istifadəçilər, tarixçə və aylıq istifadə JSON faylına (backend qovluğundan işə salın).

    python backup_db.py backup               backend/backups/az-ocr-YYYYMMDD-HHMM.json faylını yaradır
    python backup_db.py restore fayl.json    yalnız çatışmayan qeydləri bərpa edir (mövcud heç nəyi silmir, üstünə yazmır)

Hansı bazaya qoşulacağı backend/.env-dəki DATABASE_URL ilə müəyyən olunur (yoxdursa lokal SQLite faylı).
Fayl parol hash-lərini və istifadəçi mətnlərini ehtiva edir: gizli saxlayın, GitHub-a atmayın (backups/ .gitignore-dadır).
İşlər (jobs) ehtiyata daxil deyil: onlar müvəqqətidir.
"""
import json
import os
import sys
import time

TABLES = ("users", "history", "usage")


def make_backup() -> dict:
    import db
    from sqlalchemy import select
    out = {"version": 1, "created": time.time(), "tables": {}}
    with db.engine.connect() as c:
        for name in TABLES:
            tbl = db.metadata.tables[name]
            out["tables"][name] = [dict(r) for r in c.execute(select(tbl)).mappings().all()]
    return out


def restore_backup(data: dict) -> dict:
    """Çatışmayan qeydləri əlavə edir. Qaytarır: {cədvəl: əlavə olunan sayı}."""
    import db
    from sqlalchemy import insert, select, text
    if data.get("version") != 1:
        raise ValueError("Fayl formatı tanınmadı.")
    added = {}
    users = db.metadata.tables["users"]
    with db.engine.begin() as c:
        for name in TABLES:
            tbl = db.metadata.tables[name]
            keys = [col.name for col in tbl.primary_key.columns]
            have = {tuple(r) for r in c.execute(select(*[tbl.c[k] for k in keys])).all()}
            n = 0
            for row in data["tables"].get(name, []):
                if tuple(row[k] for k in keys) in have:
                    continue
                if name == "users":   # ad və ya e-poçt başqa hesabdadırsa toxunmuruq
                    clash = c.execute(select(users.c.id).where(
                        (users.c.username == row["username"]) |
                        ((users.c.email == row["email"]) if row.get("email") else False))).first()
                    if clash:
                        continue
                c.execute(insert(tbl).values(**row))
                n += 1
            added[name] = n
        if db.engine.dialect.name == "postgresql":   # nömrələyici bərpa olunmuş ən böyük nömrədən davam etsin
            c.execute(text("SELECT setval(pg_get_serial_sequence('users','id'), "
                           "GREATEST((SELECT COALESCE(MAX(id), 1) FROM users), 1))"))
    return added


def cli(argv) -> int:
    if not argv or argv[0] not in ("backup", "restore"):
        print(__doc__)
        return 1
    from dotenv import load_dotenv
    load_dotenv()
    import db
    db.init_db()
    print("Baza:", "SQLite (lokal)" if db.IS_SQLITE else "xarici Postgres")
    if argv[0] == "backup":
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("az-ocr-%Y%m%d-%H%M.json"))
        data = make_backup()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(path, "|", ", ".join(f"{k}: {len(v)}" for k, v in data["tables"].items()))
        return 0
    if len(argv) < 2:
        print("Fayl yolunu yazın: python backup_db.py restore fayl.json")
        return 1
    with open(argv[1], encoding="utf-8") as f:
        added = restore_backup(json.load(f))
    print("Bərpa olundu:", ", ".join(f"{k}: {v}" for k, v in added.items()))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
