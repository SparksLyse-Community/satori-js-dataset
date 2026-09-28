// validate.mjs — valide chaque ligne d'un dataset JSONL {prompt, jsx} en le
// rendant réellement via Satori + Resvg, avec un pool de polices multiples
// (chaque entrée peut référencer plusieurs fontFamily différentes).
//
// Usage: node validate.mjs <input.jsonl> --fonts-dir fonts --pool font_pool.json \
//          [--png-dir out_png] [--report report.json]

import fs from "node:fs";
import path from "node:path";
import satori from "satori";
import { Resvg } from "@resvg/resvg-js";
import * as babel from "@babel/core";

const args = process.argv.slice(2);
const inputPath = args[0];
if (!inputPath) {
  console.error("Usage: node validate.mjs <dataset.jsonl> --fonts-dir fonts --pool font_pool.json [--png-dir dir] [--report report.json]");
  process.exit(1);
}
const getArg = (name, def) => {
  const i = args.indexOf(name);
  return i !== -1 ? args[i + 1] : def;
};
const fontsDir = getArg("--fonts-dir", "fonts");
const poolPath = getArg("--pool", "font_pool.json");
const pngDir = getArg("--png-dir", null);
const reportPath = getArg("--report", "report.json");
if (pngDir) fs.mkdirSync(pngDir, { recursive: true });

// --- Charge le pool de polices : family -> [{data, weight, style}] ---
const WEIGHT_MAP = {
  Thin: 100, ExtraLight: 200, Light: 300, Regular: 400, Medium: 500,
  SemiBold: 600, Bold: 700, ExtraBold: 800, Black: 900,
};

function loadFontPool(poolJsonPath, dir) {
  const poolMeta = JSON.parse(fs.readFileSync(poolJsonPath, "utf-8")).pool;
  const byBase = Object.fromEntries(poolMeta.map((p) => [p.file_base, p.family]));
  const families = {}; // family -> [{data, weight, style}]
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith(".ttf") && !file.endsWith(".otf")) continue;
    const m = file.match(/^([A-Za-z0-9]+)-([A-Za-z]+)\.(ttf|otf)$/);
    if (!m) continue;
    const [, base, variant] = m;
    const family = byBase[base];
    if (!family) continue; // fichier hors pool déclaré, ignoré
    let style = "normal";
    if (variant.includes("Italic")) style = "italic";
    const weightWord = variant.replace("Italic", "") || "Regular";
    const weight = WEIGHT_MAP[weightWord] ?? 400;
    const data = fs.readFileSync(path.join(dir, file));
    families[family] = families[family] || [];
    families[family].push({ data, weight, style });
  }
  return families; // { "Work Sans": [{data,weight,style}, ...], ... }
}

const fontPool = loadFontPool(poolPath, fontsDir);
const knownFamilies = new Set(Object.keys(fontPool));
console.log(`Pool chargé: ${knownFamilies.size} familles (${[...knownFamilies].join(", ")})`);
const DEFAULT_FAMILY = knownFamilies.has("Work Sans") ? "Work Sans" : [...knownFamilies][0];

function h(type, props, ...children) {
  const flat = children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
  return { type, props: { ...(props || {}), children: flat.length ? flat : undefined } };
}

function jsxStringToElement(jsxString) {
  const code = `(${jsxString.trim()});`;
  const { code: transpiled } = babel.transformSync(code, {
    babelrc: false,
    configFile: false,
    plugins: [["@babel/plugin-transform-react-jsx", { runtime: "classic", pragma: "h", pragmaFrag: "Fragment" }]],
  });
  const body = transpiled.replace(/^"use strict";\s*/, "").replace(/;\s*$/, "");
  const fn = new Function("h", `return (${body});`);
  return fn(h);
}

const FONT_FAMILY_RE = /fontFamily:\s*['"]([^'"]+)['"]/g;
function extractFontFamilies(jsxString) {
  const found = [];
  let m;
  FONT_FAMILY_RE.lastIndex = 0;
  while ((m = FONT_FAMILY_RE.exec(jsxString))) {
    // gère fontFamily:"Nom, fallback" -> ne garde que le premier nom
    const first = m[1].split(",")[0].trim();
    if (!found.includes(first)) found.push(first);
  }
  return found;
}

const UNSUPPORTED_CSS_HINTS = ["grid-template", "gridTemplate", "position:'sticky'", "position: 'sticky'", "animation", "transition", "@media", "float:"];
function scanForRiskyPatterns(jsxString) {
  return UNSUPPORTED_CSS_HINTS.filter((pat) => jsxString.includes(pat));
}

const lines = fs.readFileSync(inputPath, "utf-8").split("\n").filter((l) => l.trim());
const report = { total: lines.length, ok: 0, failed: 0, warnings: 0, entries: [] };

for (let i = 0; i < lines.length; i++) {
  const lineNo = i + 1;
  let entry;
  const record = { line: lineNo, status: "ok", errors: [], warnings: [], fonts_referenced: [], fonts_missing: [] };

  try {
    entry = JSON.parse(lines[i]);
  } catch (e) {
    record.status = "fail";
    record.errors.push(`JSON invalide: ${e.message}`);
    report.entries.push(record);
    report.failed++;
    continue;
  }

  const promptField = entry.prompt ?? entry.description;
  const jsx = entry.jsx;
  if (!promptField) record.warnings.push("champ 'prompt' manquant ou vide");
  if (!jsx || typeof jsx !== "string") {
    record.status = "fail";
    record.errors.push("champ 'jsx' manquant ou vide");
    report.entries.push(record);
    report.failed++;
    continue;
  }

  const risky = scanForRiskyPatterns(jsx);
  if (risky.length) record.warnings.push(`motifs CSS non supportés par Satori: ${risky.join(", ")}`);

  const referenced = extractFontFamilies(jsx);
  record.fonts_referenced = referenced;
  const missing = referenced.filter((f) => !knownFamilies.has(f));
  record.fonts_missing = missing;
  if (missing.length) record.warnings.push(`polices hors pool (non rendables telles quelles): ${missing.join(", ")}`);

  let element;
  try {
    element = jsxStringToElement(jsx);
  } catch (e) {
    record.status = "fail";
    record.errors.push(`Erreur de parsing JSX: ${e.message}`);
    report.entries.push(record);
    report.failed++;
    continue;
  }

  const width = element?.props?.style?.width || 1200;
  const height = element?.props?.style?.height || 630;

  // Construit la liste de polices satori: celles du pool réellement référencées,
  // + une police par défaut en secours pour que Satori ait toujours au moins une face.
  const familiesToLoad = new Set(referenced.filter((f) => knownFamilies.has(f)));
  familiesToLoad.add(DEFAULT_FAMILY);
  const satoriFonts = [];
  for (const fam of familiesToLoad) {
    for (const face of fontPool[fam]) {
      satoriFonts.push({ name: fam, data: face.data, weight: face.weight, style: face.style });
    }
  }

  try {
    const svg = await satori(element, { width, height, fonts: satoriFonts });
    if (pngDir) {
      const resvg = new Resvg(svg, { fitTo: { mode: "width", value: width } });
      const png = resvg.render().asPng();
      fs.writeFileSync(path.join(pngDir, `line-${String(lineNo).padStart(3, "0")}.png`), png);
    }
  } catch (e) {
    record.status = "fail";
    record.errors.push(`Erreur de rendu Satori/Resvg: ${e.message}`);
    report.entries.push(record);
    report.failed++;
    continue;
  }

  if (record.warnings.length) report.warnings++;
  report.ok++;
  report.entries.push(record);
}

fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

console.log(`\n=== Validation terminée ===`);
console.log(`Total: ${report.total} | OK: ${report.ok} | Échecs: ${report.failed} | Avertissements: ${report.warnings}`);
if (report.failed > 0) {
  console.log(`\nLignes en échec:`);
  for (const e of report.entries) if (e.status === "fail") console.log(`  L${e.line}: ${e.errors.join(" | ")}`);
}
const withMissingFonts = report.entries.filter((e) => e.fonts_missing.length);
if (withMissingFonts.length) {
  console.log(`\n${withMissingFonts.length} ligne(s) référencent des polices hors pool:`);
  for (const e of withMissingFonts) console.log(`  L${e.line}: ${e.fonts_missing.join(", ")}`);
}
console.log(`\nRapport détaillé: ${reportPath}`);
