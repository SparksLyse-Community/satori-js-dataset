#!/usr/bin/env python3
"""
generate_new_entries.py — génère de nouvelles entrées {prompt, jsx, fonts}
originales (pas des traductions) via un modèle servi par Open WebUI, en
échantillonnant des briefs de design variés (type de mise en page + palette)
pour la diversité, et en contraignant le modèle à n'utiliser que des polices
du pool curé.

Prérequis: aucune dépendance externe (urllib seul).

Configuration:
    export OPENWEBUI_BASE_URL=http://localhost:3000   # sans /api/... à la fin
    export OPENWEBUI_API_KEY=owui-...                 # Paramètres -> Compte -> Clés API

Usage:
    python generate_new_entries.py --n 40 --lang fr --model llama3.1 \
        --pool font_pool.json --out new_entries.jsonl

Étape suivante obligatoire: node validate.mjs sur le fichier produit, puis
prepare_dataset.py pour fusionner avec l'existant.
"""
import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

from openwebui_client import chat

LANG_NAMES = {
    "fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand",
    "it": "italien", "pt": "portugais", "ja": "japonais", "zh": "chinois",
}

LAYOUT_TYPES = [
    "landing page SaaS en deux colonnes", "page de tarifs (pricing) à 3 formules",
    "affiche d'exposition carrée coupée en deux", "maquette d'écran mobile (app bancaire ou fitness)",
    "tableau de bord analytique avec barre latérale", "tableau kanban à 3 colonnes",
    "fiche produit e-commerce en deux colonnes", "carte de profil professionnel verticale",
    "article de blog en colonne étroite", "billet d'événement vertical",
    "page de portfolio en grille asymétrique", "newsletter email verticale",
    "écran de connexion coupé en deux", "fil d'actualité social vertical",
    "carte météo horizontale", "page d'erreur 404 centrée",
    "fiche recette verticale", "facture professionnelle verticale",
    "interface de messagerie verticale", "carte de citation minimaliste",
    "bannière de lancement produit", "page de checkout e-commerce",
    "certificat de complétion horizontal", "carte de statistiques (stat card) compacte",
]

PALETTES = [
    "nuit violette (sombre): fond #0B1020, accent #7C5CFF, accent secondaire #22D3EE",
    "crème éditoriale (clair): fond #FBF7F0, cartes #FFFFFF, accent #C2410C",
    "menthe fraîche (clair): fond #ECFDF5, accent #059669, accent secondaire #0EA5E9",
    "océan dégradé (sombre): fond #04263B, panneaux #0A3A56, accent #31C6E8",
    "noir & or (sombre): fond #0C0A09, panneau #171310, accent #D4A24C",
    "graphite néon (sombre): fond #111114, accent #C6FF3D, accent secondaire #FF3D8B",
    "sauge minimaliste (clair): fond #F3F6F2, accent #2F6F4E, accent secondaire #A3C9A8",
    "lavande douce (clair): fond #F5F3FF, accent #6D28D9, accent secondaire #EC4899",
    "rose pastel (clair): fond #FDF2F8, accent #DB2777",
    "terre cuite (clair): fond #F7F0E8, accent #B45309, accent secondaire #4D7C0F",
    "bleu nuit corporate (sombre): fond #0A1628, panneaux #12213A, accent #2E86FF",
]

SYSTEM_TEMPLATE = """Tu génères des paires (description, JSX) pour un dataset d'entraînement
de modèles de génération de mises en page rendues par Satori (https://github.com/vercel/satori).

Contraintes JSX strictes (Satori ne supporte qu'un sous-ensemble de flexbox):
- display:'flex' EXPLICITE sur chaque élément, y compris les divs texte simples
- Jamais de grid, position:sticky, animation, transition, @media, float
- Tailles en nombres (px implicite) ou chaînes simples ('16px 24px')
- Couleurs en hexadécimal #RRGGBB uniquement
- fontFamily DOIT être une valeur EXACTE de cette liste (jamais d'autre nom):
{font_list}
- Un design peut légitimement combiner 1 à 3 polices différentes (ex: une police display
  pour le titre, une police texte pour le corps, une police mono pour un prix/une métadonnée)
  choisies de façon cohérente avec le style de la police (voir tags de style ci-dessus)

La langue du texte (description ET tout texte visible dans le JSX) doit être: {lang_name}.

Réponds STRICTEMENT en JSON, sans balises markdown, sans texte avant/après, sous la
forme exacte: {{"prompt": "description complète et précise, structure + palette + contenu", "jsx": "..."}}"""


def build_font_list(pool_path):
    pool = json.loads(Path(pool_path).read_text(encoding="utf-8"))["pool"]
    return "\n".join(f"  - \"{p['family']}\" ({p['style_tag']})" for p in pool)


def extract_fonts(jsx: str):
    found = []
    for m in re.findall(r"fontFamily:\s*['\"]([^'\"]+)['\"]", jsx):
        first = m.split(",")[0].strip()
        if first not in found:
            found.append(first)
    return found


def parse_json_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def generate_one(base_url, api_key, model, system, lang_name, max_retries=3):
    layout = random.choice(LAYOUT_TYPES)
    palette = random.choice(PALETTES)
    n_fonts = random.choice([1, 1, 2, 2, 3])
    brief = (
        f"Brief de design (en {lang_name}):\n"
        f"- Type de mise en page: {layout}\n"
        f"- Palette: {palette}\n"
        f"- Utilise {n_fonts} police(s) différente(s) parmi le pool, choisies pour leur cohérence avec le style.\n"
        f"- Invente un contenu textuel crédible et varié (marque, noms, chiffres) dans la langue demandée."
    )
    for attempt in range(max_retries):
        try:
            text = chat(base_url, api_key, model, system, brief)
            parsed = parse_json_response(text)
            jsx = parsed["jsx"]
            return {"prompt": parsed["prompt"], "jsx": jsx, "fonts": extract_fonts(jsx)}
        except Exception as e:
            if attempt == max_retries - 1:
                return {"_error": str(e), "_layout": layout, "_palette": palette}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True, help="nombre d'entrées à générer")
    ap.add_argument("--lang", default="fr", help="code langue (fr, en, es, de, ...)")
    ap.add_argument("--pool", default="font_pool.json")
    ap.add_argument("--out", default="dataset.jsonl")
    ap.add_argument("--model", required=True, help="nom du modèle tel que configuré dans Open WebUI, ex: llama3.1")
    ap.add_argument("--base-url", default=os.environ.get("OPENWEBUI_BASE_URL"))
    ap.add_argument("--api-key", default=os.environ.get("OPENWEBUI_API_KEY"))
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("Renseigne --base-url/--api-key ou OPENWEBUI_BASE_URL/OPENWEBUI_API_KEY", file=sys.stderr)
        sys.exit(1)

    font_list = build_font_list(args.pool)
    lang_name = LANG_NAMES.get(args.lang, args.lang)
    system = SYSTEM_TEMPLATE.format(font_list=font_list, lang_name=lang_name)

    results, errors = [], []
    for i in range(args.n):
        out = generate_one(args.base_url, args.api_key, args.model, system, lang_name)
        if out and "_error" in out:
            errors.append(out)
        elif out:
            out["lang"] = args.lang
            results.append(out)
        if (i + 1) % 5 == 0 or i + 1 == args.n:
            print(f"  {i + 1}/{args.n} généré ({len(errors)} échecs)")

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if errors:
        err_path = Path(args.out).with_suffix(".errors.jsonl")
        with open(err_path, "w", encoding="utf-8") as f:
            for e in errors:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"⚠️  {len(errors)} générations échouées — voir {err_path}")

    print(f"\n{len(results)} nouvelles entrées écrites dans {args.out}")
    print("⚠️  Étape suivante obligatoire: node validate.mjs pour filtrer ce qui ne rend pas réellement.")


if __name__ == "__main__":
    main()