"""İstifadəçiləri idarə etmək (backend qovluğundan işə salın).

    python manage_users.py list              hamısına bax
    python manage_users.py usage             bu ayın Azure səhifə istifadəsi (USER_MONTHLY_PAGES limiti üçün)
    python manage_users.py add ad            yeni hesab (parolu və e-poçtu soruşur)
    python manage_users.py setemail ad poçt  hesaba e-poçt yaz (köhnə hesablar üçün; parol bərpası bu ünvana gedir)
    python manage_users.py passwd ad         parolu dəyişdir (köhnə girişlər avtomatik bağlanır)
    python manage_users.py delete ad         hesabı və onun tarixçəsini sil

Hansı bazaya qoşulacağı backend/.env-dəki DATABASE_URL ilə müəyyən olunur
(yoxdursa lokal SQLite faylı).
"""
import getpass
import os
import sys
import time

# main.py import olunanda bu dəyişənlər lazımdır; burada həqiqi Azure çağırışı olmur
os.environ.setdefault("AZURE_DI_ENDPOINT", "https://example.cognitiveservices.azure.com/")
os.environ.setdefault("AZURE_DI_KEY", "manage-users")

import db  # noqa: E402
import main  # noqa: E402


def ask_password() -> str:
    while True:
        first = getpass.getpass("Yeni parol (ən azı 8 simvol): ")
        if not 8 <= len(first) <= 128:
            print("Parol 8-128 simvol olmalıdır.")
            continue
        if first != getpass.getpass("Parolu təkrar yazın: "):
            print("Parollar eyni deyil.")
            continue
        return first


def main_cli(argv):
    if not argv or argv[0] not in ("list", "usage", "add", "passwd", "delete", "setemail"):
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "list":
        rows = db.list_users()
        if not rows:
            print("İstifadəçi yoxdur.")
        for u in rows:
            print(f'{u["id"]:>3}  {u["display"]:<32} {(u.get("email") or "-"):<36} {time.strftime("%Y-%m-%d %H:%M", time.localtime(u["created"]))}')
        return 0
    if cmd == "usage":
        month = main._month()
        used = db.usage_list(month)
        print(f"Ay: {month} | limit: {main.USER_MONTHLY_PAGES or 'yoxdur'}")
        for u in db.list_users():
            print(f'{u["id"]:>3}  {u["display"]:<32} {used.get(u["id"], 0):>5} səhifə')
        return 0
    if len(argv) < 2:
        print("İstifadəçi adını yazın.")
        return 1
    name = argv[1].strip()
    key = name.casefold()
    if cmd == "add":
        if not main.USERNAME_RE.match(name):
            print("İstifadəçi adı 3-32 simvol olmalıdır: hərf, rəqəm, nöqtə, tire, alt xətt.")
            return 1
        email = input("E-poçt (boş buraxmaq olar, amma parol bərpası işləməyəcək): ").strip().casefold()
        if email and not main.EMAIL_RE.match(email):
            print("E-poçt ünvanı düzgün deyil.")
            return 1
        if email and db.get_user_by_email(email):
            print("Bu e-poçt artıq istifadə olunur.")
            return 1
        uid = db.create_user(key, name, main.hash_password(ask_password()), email)
        if uid is None:
            print("Bu ad artıq var.")
            return 1
        if email:
            db.set_email_verified(uid)   # administrator yaradıb: təsdiq linki tələb olunmur
        print("Hesab yaradıldı.")
    elif cmd == "setemail":
        email = argv[2].strip().casefold() if len(argv) > 2 else ""
        if not main.EMAIL_RE.match(email):
            print("İkinci söz e-poçt olmalıdır: python manage_users.py setemail ad ad@example.com")
            return 1
        other = db.get_user_by_email(email)
        if other and other["username"] != key:
            print("Bu e-poçt başqa hesabda var.")
            return 1
        print("E-poçt yazıldı." if db.set_email(key, email) else "Belə istifadəçi tapılmadı.")
    elif cmd == "passwd":
        if not db.set_password(key, main.hash_password(ask_password())):
            print("Belə istifadəçi tapılmadı.")
            return 1
        print("Parol dəyişdirildi.")
    elif cmd == "delete":
        if input(f'"{name}" hesabı və bütün tarixçəsi silinəcək. Davam üçün "hə" yazın: ').strip().lower() != "hə":
            print("Ləğv edildi.")
            return 0
        print("Silindi." if db.delete_user(key) else "Belə istifadəçi tapılmadı.")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv[1:]))
