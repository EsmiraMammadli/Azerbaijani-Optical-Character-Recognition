"""OCR keyfiyyətinin ölçülməsi: xam Azure OCR və Azure + LLM üçün CER/WER.

İstifadə (backend qovluğundan):
    python evaluate.py
    python evaluate.py --enhance        # skan təmizləmə ilə
    python evaluate.py --no-llm         # yalnız xam Azure OCR

Qovluq quruluşu:
    eval/samples/<ad>.jpg|png|pdf|...   sınaq sənədləri
    eval/ground_truth/<ad>.txt          əl ilə yazılmış düzgün mətn (UTF-8)

Nəticə: eval/results.md, eval/results.csv və eval/letters.md (xüsusi hərflər üzrə: Ə, ı, İ, ö, ü, ç, ş, ğ, q, x, j)
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter

import jiwer

from main import ALLOWED_EXT, DOCUMENT_EXT, process_bytes

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "eval", "samples")
TRUTH = os.path.join(HERE, "eval", "ground_truth")


def norm(text: str) -> str:
    """Boşluqları və sətir keçidlərini eyniləşdirir (sətir bölgüsü ölçüyə təsir etməsin)."""
    return re.sub(r"\s+", " ", text or "").strip()


def score(ref: str, hyp: str):
    ref, hyp = norm(ref), norm(hyp)
    if not ref:
        return None, None
    return jiwer.cer(ref, hyp), jiwer.wer(ref, hyp)


SPECIAL = "ƏəİıÖöÜüÇçŞşĞğQqXxJj"   # Azərbaycan dilinə xas və tez qarışdırılan hərflər


def letter_stats(pairs):
    """pairs: [(düzgün_mətn, oxunan_mətn)]. Hər xüsusi hərf üçün [cəmi, düzgün, Counter(nəyə çevrilib)] qaytarır.
    "∅" hərfin oxunan mətndə düşdüyü deməkdir."""
    stats = {ch: [0, 0, Counter()] for ch in SPECIAL}
    for ref, hyp in pairs:
        ref, hyp = norm(ref), norm(hyp)
        if not ref:
            continue
        out = jiwer.process_characters(ref, hyp)
        for c in out.alignments[0]:
            for k in range(c.ref_start_idx, c.ref_end_idx):
                ch = ref[k]
                if ch not in stats:
                    continue
                st = stats[ch]
                st[0] += 1
                if c.type == "equal":
                    st[1] += 1
                elif c.type == "substitute":
                    st[2][hyp[c.hyp_start_idx + (k - c.ref_start_idx)]] += 1
                else:
                    st[2]["∅"] += 1
    return stats


def letters_table(stats) -> str:
    lines = ["| Hərf | Sayı | Düzgün | Ən çox necə oxunub |", "|---|---|---|---|"]
    for ch in SPECIAL:
        total, ok, errs = stats[ch]
        if not total:
            continue
        top = ", ".join(f"`{c}` ×{n}" for c, n in errs.most_common(3)) or "-"
        lines.append(f"| {ch} | {total} | {ok / total * 100:.0f}% | {top} |")
    return "\n".join(lines)


def pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enhance", action="store_true", help="skan təmizləmə (OpenCV)")
    ap.add_argument("--no-llm", action="store_true", help="LLM mərhələsini keç")
    args = ap.parse_args()

    names = sorted(
        f for f in os.listdir(SAMPLES)
        if os.path.splitext(f)[1].lower() in ALLOWED_EXT
        and os.path.splitext(f)[1].lower() not in DOCUMENT_EXT
    )
    if not names:
        sys.exit(f"{SAMPLES} qovluğunda sənəd yoxdur.")

    rows, skipped = [], []
    raw_pairs, fix_pairs = [], []
    for name in names:
        stem = os.path.splitext(name)[0]
        truth_path = os.path.join(TRUTH, stem + ".txt")
        if not os.path.exists(truth_path):
            skipped.append(f"{name}: ground_truth/{stem}.txt tapılmadı")
            continue
        with open(truth_path, encoding="utf-8") as f:
            truth = f.read()
        with open(os.path.join(SAMPLES, name), "rb") as f:
            data = f.read()

        print(f"-> {name} ...", flush=True)
        try:
            res = process_bytes(name, data, fix=not args.no_llm, enhance=args.enhance)
        except Exception as e:  # HTTPException və s.
            skipped.append(f"{name}: {getattr(e, 'detail', e)}")
            continue

        raw_cer, raw_wer = score(truth, res["raw_text"])
        llm_ok = res["llm_fixed"]
        raw_pairs.append((truth, res["raw_text"]))
        if llm_ok:
            fix_pairs.append((truth, res["text"]))
        fix_cer, fix_wer = score(truth, res["text"]) if llm_ok else (None, None)
        rows.append({
            "file": name, "method": res["method"], "ref_chars": len(norm(truth)),
            "raw_cer": raw_cer, "raw_wer": raw_wer, "fix_cer": fix_cer, "fix_wer": fix_wer,
            "llm_error": res.get("llm_error"),
        })

    if not rows:
        sys.exit("Ölçüləcək sənəd qalmadı:\n" + "\n".join(skipped))

    def avg(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) / len(vals) if vals else None

    lines = [
        "| Sənəd | Metod | Xam CER | Xam WER | LLM sonrası CER | LLM sonrası WER |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['file']} | {r['method']} | {pct(r['raw_cer'])} | {pct(r['raw_wer'])} "
            f"| {pct(r['fix_cer'])} | {pct(r['fix_wer'])} |"
        )
    lines.append(
        f"| **Orta ({len(rows)} sənəd)** | | **{pct(avg('raw_cer'))}** | **{pct(avg('raw_wer'))}** "
        f"| **{pct(avg('fix_cer'))}** | **{pct(avg('fix_wer'))}** |"
    )
    table = "\n".join(lines)

    notes = []
    if args.enhance:
        notes.append("Skan təmizləmə (OpenCV) açıq idi.")
    if args.no_llm:
        notes.append("LLM mərhələsi keçildi.")
    errs = [f"{r['file']}: {r['llm_error']}" for r in rows if r["llm_error"]]
    if errs:
        notes.append("LLM xətası olan sənədlər (onlar üçün LLM sütunu boşdur): " + "; ".join(errs))
    notes += [f"Keçildi: {s}" for s in skipped]

    out = table + ("\n\n" + "\n".join(f"- {n}" for n in notes) if notes else "") + "\n"
    print("\n" + out)

    with open(os.path.join(HERE, "eval", "results.md"), "w", encoding="utf-8") as f:
        f.write(out)
    letters = "### Xam Azure OCR\n\n" + letters_table(letter_stats(raw_pairs)) + "\n"
    if fix_pairs:
        letters += "\n### LLM düzəlişindən sonra\n\n" + letters_table(letter_stats(fix_pairs)) + "\n"
    print("\nXüsusi hərflər üzrə nəticə:\n\n" + letters)
    with open(os.path.join(HERE, "eval", "letters.md"), "w", encoding="utf-8") as f:
        f.write(letters)
    with open(os.path.join(HERE, "eval", "results.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("Yazıldı: eval/results.md, eval/results.csv, eval/letters.md")


if __name__ == "__main__":
    main()