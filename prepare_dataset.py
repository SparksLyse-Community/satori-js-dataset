#!/usr/bin/env python3
"""
prepare_dataset.py — transforme un dataset brut {description/prompt, jsx} (JSONL)
en dataset plat {"prompt", "jsx", "fonts"} prêt pour l'entraînement, en excluant
les entrées qui ont échoué à la validation Satori (report.json de validate.mjs).

Usage:
    python prepare_dataset.py raw.jsonl --report report.json --pool font_pool.json --out-dir prepared/

Sorties (dans --out-dir):
    dataset.jsonl   -> [{"prompt": ..., "jsx": ..., "fonts": [...]}]
    stats.json      -> rapport qualité (doublons, longueurs, diversité couleurs/polices)
    rejected.jsonl  -> entrées exclues avec la raison
"""
import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
FONT_FAMILY_RE = re.compile(r"fontFamily:\s*['\"]([^'\"]+)['\"]")
RISKY_PATTERNS = ["grid-template", "gridTemplate", "position:'sticky'", "position: 'sticky'",
                   "animation", "transition", "@media", "float:"]


def load_report(report_path):
    if not report_path or not Path(report_path).exists():
        return {}
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return {e["line"]: e for e in data.get("entries", [])}


def load_font_pool(pool_path):
    if not pool_path or not Path(pool_path).exists():
        return None
    data = json.loads(Path(pool_path).read_text(encoding="utf-8"))
    return {p["family"] for p in data.get("pool", [])}


def extract_fonts(jsx: str):
    found = []
    for m in FONT_FAMILY_RE.findall(jsx):
        first = m.split(",")[0].strip()
        if first not in found:
            found.append(first)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="dataset brut JSONL {prompt|description, jsx}")
    ap.add_argument("--report", help="report.json produit par validate.mjs (recommandé)")
    ap.add_argument("--pool", help="font_pool.json pour vérifier que les polices référencées existent")
    ap.add_argument("--out-dir", default="prepared")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_by_line = load_report(args.report)
    known_fonts = load_font_pool(args.pool)

    raw_lines = [l for l in Path(args.input).read_text(encoding="utf-8").splitlines() if l.strip()]

    kept = []
    rejected = []
    seen_hashes = {}
    prompt_lengths, jsx_lengths = [], []
    all_hex_colors = Counter()
    risky_hits = Counter()
    font_usage = Counter()
    multi_font_count = 0
    unknown_font_hits = Counter()

    for i, line in enumerate(raw_lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            rejected.append({"line": i, "reason": f"JSON invalide: {e}"})
            continue

        prompt = (entry.get("prompt") or entry.get("description") or "").strip()
        jsx = (entry.get("jsx") or "").strip()

        if not prompt or not jsx:
            rejected.append({"line": i, "reason": "prompt ou jsx manquant", "entry": entry})
            continue

        val = report_by_line.get(i)
        if val and val.get("status") == "fail":
            rejected.append({"line": i, "reason": f"échec validation Satori: {val.get('errors')}", "entry": entry})
            continue

        jsx_hash = hashlib.sha256(jsx.encode()).hexdigest()
        if jsx_hash in seen_hashes:
            rejected.append({"line": i, "reason": f"doublon exact du JSX (ligne {seen_hashes[jsx_hash]})", "entry": entry})
            continue
        seen_hashes[jsx_hash] = i

        fonts = extract_fonts(jsx)
        if known_fonts is not None:
            unknown = [f for f in fonts if f not in known_fonts]
            for f in unknown:
                unknown_font_hits[f] += 1

        prompt_lengths.append(len(prompt))
        jsx_lengths.append(len(jsx))
        for hexcode in HEX_RE.findall(jsx):
            all_hex_colors[hexcode.upper()] += 1
        for pat in RISKY_PATTERNS:
            if pat in jsx:
                risky_hits[pat] += 1
        for f in fonts:
            font_usage[f] += 1
        if len(fonts) >= 2:
            multi_font_count += 1

        kept.append({"prompt": prompt, "jsx": jsx, "fonts": fonts})

    with open(out_dir / "dataset.jsonl", "w", encoding="utf-8") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with open(out_dir / "rejected.jsonl", "w", encoding="utf-8") as f:
        for row in rejected:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    stats = {
        "total_input_lines": len(raw_lines),
        "kept": len(kept),
        "rejected": len(rejected),
        "rejection_reasons": Counter(r["reason"].split(":")[0] for r in rejected).most_common(),
        "prompt_length_chars": {
            "min": min(prompt_lengths) if prompt_lengths else None,
            "max": max(prompt_lengths) if prompt_lengths else None,
            "mean": round(statistics.mean(prompt_lengths), 1) if prompt_lengths else None,
        },
        "jsx_length_chars": {
            "min": min(jsx_lengths) if jsx_lengths else None,
            "max": max(jsx_lengths) if jsx_lengths else None,
            "mean": round(statistics.mean(jsx_lengths), 1) if jsx_lengths else None,
        },
        "unique_hex_colors_used": len(all_hex_colors),
        "top_20_hex_colors": all_hex_colors.most_common(20),
        "font_usage_count": dict(font_usage.most_common()),
        "entries_using_2plus_fonts": multi_font_count,
        "entries_using_1_font": sum(1 for r in kept if len(r["fonts"]) == 1),
        "entries_using_0_font": sum(1 for r in kept if len(r["fonts"]) == 0),
        "unknown_fonts_outside_pool": dict(unknown_font_hits),
        "risky_css_patterns_found": dict(risky_hits),
    }
    with open(out_dir / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"Gardés: {len(kept)} / {len(raw_lines)}  |  Rejetés: {len(rejected)}")
    print(f"Entrées multi-fonts (2+): {multi_font_count}  |  1 police: {stats['entries_using_1_font']}  |  0 police: {stats['entries_using_0_font']}")
    print(f"Polices utilisées: {dict(font_usage.most_common())}")
    if unknown_font_hits:
        print(f"⚠️  Polices référencées hors pool: {dict(unknown_font_hits)} — ces entrées ne rendront pas correctement telles quelles.")
    if risky_hits:
        print(f"⚠️  Motifs CSS risqués détectés: {dict(risky_hits)}")
    print(f"\nFichiers écrits dans {out_dir}/: dataset.jsonl, stats.json, rejected.jsonl")


if __name__ == "__main__":
    sys.exit(main())
