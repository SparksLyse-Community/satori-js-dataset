#!/usr/bin/env python3
"""
translate_dataset.py — traduit un dataset {prompt, jsx, fonts} existant vers
plusieurs langues via un modèle servi par Open WebUI, en ne touchant qu'au
texte visible (jamais la structure/styles/fontFamily/couleurs).

Multiplie le dataset ET le rend multilingue en une passe : chaque entrée
d'origine (taguée lang="fr") produit une variante par langue cible.

Prérequis: aucune dépendance externe (urllib seul).

Configuration:
    export OPENWEBUI_BASE_URL=http://localhost:3000   # sans /api/... à la fin
    export OPENWEBUI_API_KEY=owui-...                 # Paramètres -> Compte -> Clés API

Usage:
    python translate_dataset.py dataset.jsonl --langs en,es,de \
        --model llama3.1 --out dataset_multilang.jsonl

Puis, comme pour tout ajout au dataset : faire revalider les nouvelles lignes
via validate.mjs avant de les considérer comme définitives.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
from pathlib import Path

from openwebui_client import chat, OpenWebUIError

LANG_NAMES = {
    "en": "anglais", "es": "espagnol", "de": "allemand", "it": "italien",
    "pt": "portugais", "nl": "néerlandais", "ja": "japonais", "ko": "coréen",
    "zh": "chinois", "ar": "arabe", "fr": "français",
}

SYSTEM = """Tu traduis des paires (description, JSX Satori) pour un dataset d'entraînement.

Règles ABSOLUES:
1. Traduis "prompt" intégralement dans la langue cible.
2. Dans "jsx", traduis UNIQUEMENT le texte visible affiché à l'écran (labels de boutons,
   titres, paragraphes, noms de personnes fictifs si pertinent culturellement, libellés).
3. Ne touche JAMAIS: les noms de balises, les propriétés de style (fontSize, padding,
   backgroundColor, etc.), les valeurs numériques, les couleurs hexadécimales, les noms
   de police dans fontFamily (garde exactement les mêmes chaînes), la structure JSX,
   l'indentation, les guillemets utilisés pour les attributs.
4. Adapte les formats culturels si besoin (devise, format de date) SEULEMENT si le texte
   original en contient un affiché — ne change rien d'autre.
5. Réponds STRICTEMENT en JSON, sans balises markdown, sans texte avant/après, sous la
   forme exacte: {"prompt": "...", "jsx": "..."}"""


def extract_fonts(jsx: str):
    found = []
    for m in re.findall(r"fontFamily:\s*['\"]([^'\"]+)['\"]", jsx):
        first = m.split(",")[0].strip()
        if first not in found:
            found.append(first)
    return found


def structural_fingerprint(jsx: str):
    """Signature grossière de la structure, pour détecter une traduction qui a cassé le JSX."""
    return (
        jsx.count("<div"),
        jsx.count("style={{"),
        tuple(sorted(re.findall(r"#[0-9A-Fa-f]{6}", jsx))),
        tuple(extract_fonts(jsx)),
    )


def parse_json_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def translate_one(base_url, api_key, model, entry, lang_code, max_retries=3):
    lang_name = LANG_NAMES.get(lang_code, lang_code)
    user_msg = (
        f"Langue cible: {lang_name} ({lang_code})\n\n"
        f"prompt: {entry['prompt']}\n\n"
        f"jsx: {entry['jsx']}"
    )
    original_fp = structural_fingerprint(entry["jsx"])

    for attempt in range(max_retries):
        try:
            text = chat(base_url, api_key, model, SYSTEM, user_msg)
            parsed = parse_json_response(text)
            new_jsx = parsed["jsx"]
            if structural_fingerprint(new_jsx) != original_fp:
                raise ValueError("structure/couleurs/polices altérées par la traduction")
            return {
                "prompt": parsed["prompt"],
                "jsx": new_jsx,
                "fonts": entry.get("fonts") or extract_fonts(new_jsx),
                "lang": lang_code,
            }
        except Exception as e:
            if attempt == max_retries - 1:
                return {"_error": str(e), "_source_prompt": entry["prompt"][:80]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="dataset.jsonl {prompt, jsx, fonts}")
    ap.add_argument("--langs", required=True, help="codes langue cibles séparés par des virgules, ex: en,es,de")
    ap.add_argument("--out", default="dataset_multilang.jsonl")
    ap.add_argument("--model", required=True, help="nom du modèle tel que configuré dans Open WebUI, ex: llama3.1")
    ap.add_argument("--base-url", default=os.environ.get("OPENWEBUI_BASE_URL"))
    ap.add_argument("--api-key", default=os.environ.get("OPENWEBUI_API_KEY"))
    ap.add_argument("--workers", type=int, default=1, help="requêtes en parallèle (1 = séquentiel, plus sûr pour un modèle local)")
    ap.add_argument("--source-lang", default="fr")
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("Renseigne --base-url/--api-key ou OPENWEBUI_BASE_URL/OPENWEBUI_API_KEY", file=sys.stderr)
        sys.exit(1)

    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    entries = [json.loads(l) for l in Path(args.input).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"{len(entries)} entrées source × {len(langs)} langues = {len(entries) * len(langs)} traductions à faire")

    jobs = [(e, lang) for e in entries for lang in langs]
    results, errors = [], []

    def run(job):
        e, lang = job
        return translate_one(args.base_url, args.api_key, args.model, e, lang)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = 0
        for out in pool.map(run, jobs):
            done += 1
            if out and "_error" in out:
                errors.append(out)
            elif out:
                results.append(out)
            if done % 10 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} traité ({len(errors)} échecs)")

    with open(args.out, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps({**e, "lang": args.source_lang}, ensure_ascii=False) + "\n")
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if errors:
        err_path = Path(args.out).with_suffix(".errors.jsonl")
        with open(err_path, "w", encoding="utf-8") as f:
            for e in errors:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"\n⚠️  {len(errors)} traductions échouées (structure altérée ou erreur API) — voir {err_path}")

    print(f"\n{len(entries)} lignes '{args.source_lang}' + {len(results)} traductions écrites dans {args.out}")
    print("⚠️  Étape suivante obligatoire: node validate.mjs pour revalider ces nouvelles lignes avant usage.")


if __name__ == "__main__":
    main()