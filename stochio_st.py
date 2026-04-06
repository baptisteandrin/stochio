#!/usr/bin/env python3
"""
Calculateur de Stœchiométrie — Streamlit
pip install streamlit pandas pubchempy reportlab groq openpyxl google-generativeai
streamlit run stochio_st.py
Accès téléphone : http://<IP-PC>:8501  (même Wi-Fi)
"""

import io
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    import pubchempy as pcp
    _PCP_OK = True
except ImportError:
    _PCP_OK = False

try:
    import groq as groq_lib
    _GROQ_OK = True
except ImportError:
    _GROQ_OK = False

try:
    from google import genai as genai_lib
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib import colors as rl_colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# =============================================================================
# Chemins & configuration  (identiques à stochio_qt.py)
# =============================================================================
_BASE_DIR = Path(__file__).parent
INVENTAIRE_PATH = _BASE_DIR / "Inventaire.xlsx"
PUBCHEM_DB_PATH = _BASE_DIR / "DataBasePubMeb.xlsx"
CONFIG_PATH = Path.home() / ".stochio_config.json"

SOLVANTS_USUELS = [
    "Eau", "Méthanol", "Éthanol", "Isopropanol", "Acide acétique",
    "n-Butanol", "Acétone", "Acétonitrile", "DMF", "DMSO",
    "THF", "DCM", "Acétate d'éthyle", "Éther diéthylique",
    "Toluène", "Hexane", "Cyclohexane", "Pentane", "Chloroforme", "Xylène",
]

ROLES = ["Limitant", "Réactif", "Solvant", "Catalyseur", "Autre"]

ROWS_DEF = [
    ("MW (g/mol)",     "mw"),
    ("Masse (g)",      "mass_g"),
    ("n (mol)",        "mol"),
    ("Eq / Rendement", "eq"),
    ("Densité (g/mL)", "density"),
    ("Volume (mL)",    "volume"),
    ("Pureté (%)",     "purity"),
]

# =============================================================================
# Config  — clés lues depuis .streamlit/secrets.toml (jamais exposées dans l'UI)
# =============================================================================
def charger_provider():
    return st.secrets.get("ai_provider", "gemini")

def charger_api_key(provider=None):
    if provider is None:
        provider = charger_provider()
    if provider == "gemini":
        return st.secrets.get("gemini_key", "")
    return st.secrets.get("groq_key", "")

# =============================================================================
# Inventaire
# =============================================================================
def _parse_mw(mw_raw: str):
    """Parse une valeur MW quelle que soit la locale (virgule décimale, espace milliers…)."""
    s = mw_raw.strip().replace("\xa0", "").replace(" ", "")
    if not s or s.lower() == "nan":
        return None
    try:
        # Format français : 174,16 ou 1 174,16
        if "," in s and "." not in s:
            return float(s.replace(",", "."))
        # Format anglais avec milliers : 1,174.16
        if "," in s and "." in s:
            return float(s.replace(",", ""))
        return float(s)
    except ValueError:
        return None


def _charger_inventaire():
    # Lecture depuis Google Sheets si credentials disponibles
    if _GSPREAD_OK and "gcp_service_account" in st.secrets:
        try:
            creds = Credentials.from_service_account_info(
                dict(st.secrets["gcp_service_account"]),
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                        "https://www.googleapis.com/auth/drive.readonly"],
            )
            gc = gspread.authorize(creds)
            sheet_id = st.secrets.get("inventaire_sheet_id", "")
            sh = gc.open_by_key(sheet_id)
            ws = sh.get_worksheet(0)
            all_vals = ws.get_all_values()
            if not all_vals:
                return []
            headers = all_vals[0]
            rows = [dict(zip(headers, r)) for r in all_vals[1:]]
            res = []
            for row in rows:
                nom = str(row.get("Nom du Produit", "") or "").strip()
                mw_raw = str(row.get("Masse Molaire (g/mol)", "") or "").strip()
                if nom and nom.lower() != "nan":
                    res.append({"nom": nom, "mw": _parse_mw(mw_raw)})
            return res
        except Exception:
            pass
    # Fallback : fichier local
    try:
        df = pd.read_excel(INVENTAIRE_PATH, dtype=str)
        res = []
        for _, row in df.iterrows():
            nom = str(row.get("Nom du Produit", "") or "").strip()
            mw_raw = str(row.get("Masse Molaire (g/mol)", "") or "").strip()
            if nom and nom.lower() != "nan":
                try:
                    mw = float(mw_raw.replace(",", ".")) if mw_raw and mw_raw.lower() != "nan" else None
                except ValueError:
                    mw = None
                res.append({"nom": nom, "mw": mw})
        return res
    except Exception:
        return []

def _charger_pubchem_db():
    try:
        df = pd.read_excel(PUBCHEM_DB_PATH, dtype=str)
        res = []
        for _, row in df.iterrows():
            nom = str(row.get("Nom", "") or "").strip()
            mw_raw = str(row.get("MW (g/mol)", "") or "").strip()
            if nom and nom.lower() != "nan":
                try:
                    mw = float(mw_raw.replace(",", ".")) if mw_raw and mw_raw.lower() != "nan" else None
                except ValueError:
                    mw = None
                res.append({"nom": nom, "mw": mw})
        return res
    except Exception:
        return []

@st.cache_data(ttl=300)
def load_inventaire():
    inv_local = _charger_inventaire()
    inv_pc = _charger_pubchem_db()
    noms_locaux = {p["nom"].lower() for p in inv_local}
    return inv_local + [p for p in inv_pc if p["nom"].lower() not in noms_locaux]

# =============================================================================
# Utilitaires & calcul  (mêmes formules que stochio_qt.py)
# =============================================================================
def _f(v, default=0.0):
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return default

def fmt(v, d=5):
    if v is None or v == 0.0:
        return ""
    return f"{v:.{d}f}".rstrip("0").rstrip(".")

def recalc(reagents, prod):
    if not reagents:
        return None, [], {}

    lim = next((r for r in reagents if r["role"] == "Limitant"), None)
    n_lim = None
    if lim and lim["mw"] > 0 and lim["mass_g"] > 0:
        n_lim = lim["mass_g"] * (lim["purity"] / 100.0) / lim["mw"]

    results = []
    for r in reagents:
        is_lim  = r["role"] == "Limitant"
        mw      = r["mw"]
        purity  = r["purity"] or 100.0
        density = r.get("density", 0.0)

        if is_lim:
            mol    = n_lim or 0.0
            mass_g = r["mass_g"]
            eq     = 1.0
        else:
            eq     = r.get("eq", 1.0)
            mol    = eq * n_lim if n_lim else 0.0
            mass_g = mol * mw / (purity / 100.0) if (mw and purity > 0) else 0.0

        volume = (mass_g / density) if (density and mass_g) else 0.0
        results.append({"mw": mw, "mass_g": mass_g, "mol": mol,
                         "eq": eq, "density": density, "volume": volume, "purity": purity})

    prod_mw      = prod.get("mw") or 0.0
    prod_mol     = n_lim or 0.0
    prod_density = prod.get("density", 0.0)

    if prod.get("mass_manual"):
        prod_mass = prod.get("mass", 0.0)
        prod_yield = (prod_mass / (prod_mol * prod_mw)) if (prod_mol and prod_mw) else prod.get("yield", 1.0)
    else:
        prod_yield = prod.get("yield", 1.0)
        prod_mass  = prod_mol * prod_mw * prod_yield if prod_mw else 0.0

    prod_vol = (prod_mass / prod_density) if (prod_density and prod_mass) else 0.0
    prod_result = {"mw": prod_mw, "mass_g": prod_mass, "mol": prod_mol,
                   "eq": prod_yield, "density": prod_density, "volume": prod_vol, "purity": 100.0}

    return n_lim, results, prod_result

def build_display_df(reagents, results, prod_result, prod_name):
    data = {}
    for r, res in zip(reagents, results):
        data[r["name"]] = {label: fmt(res.get(key, 0)) for label, key in ROWS_DEF}
    data[prod_name or "Produit"] = {label: fmt(prod_result.get(key, 0)) for label, key in ROWS_DEF}
    return pd.DataFrame(data, index=[label for label, _ in ROWS_DEF])

# =============================================================================
# PubChem
# =============================================================================
def pc_search(query):
    if not _PCP_OK:
        return None, "pubchempy non installé"
    try:
        comps = pcp.get_compounds(query, "name")
        if not comps:
            comps = pcp.get_compounds(query, "formula")
        if not comps:
            return None, f"Aucun résultat pour « {query} »"
        c = comps[0]
        return {"name": c.iupac_name or query,
                "mw": round(float(c.molecular_weight), 4) if c.molecular_weight else None,
                "formula": c.molecular_formula or ""}, None
    except Exception as e:
        return None, str(e)

# =============================================================================
# Export PDF  (même fonction que stochio_qt.py)
# =============================================================================
def make_pdf(col_names, row_labels, data_matrix, rxn_name, procedure=""):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    header = [""] + col_names
    data = [header] + [[label] + row for label, row in zip(row_labels, data_matrix)]
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0),  rl_colors.HexColor("#2563eb")),
        ("TEXTCOLOR",  (0, 0), (-1, 0),  rl_colors.white),
        ("FONTNAME",   (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("BACKGROUND", (0, 1), (0, -1),  rl_colors.HexColor("#f1f5f9")),
        ("TEXTCOLOR",  (0, 1), (0, -1),  rl_colors.HexColor("#1e40af")),
        ("FONTNAME",   (0, 1), (0, -1),  "Helvetica-Bold"),
        ("BACKGROUND", (-1, 1), (-1, -1), rl_colors.HexColor("#f0fdf4")),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("ALIGN",      (1, 1), (-1, -1), "CENTER"),
        ("ALIGN",      (0, 0), (0, -1),  "LEFT"),
        ("GRID",       (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#cbd5e1")),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        bg = rl_colors.HexColor("#f8fafc") if i % 2 == 0 else rl_colors.white
        ts.append(("BACKGROUND", (1, i), (-2, i), bg))
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle(ts))
    elements = [Paragraph(f"Stœchiométrie — {rxn_name}", styles["Title"]),
                Spacer(1, 0.5*cm), t]
    if procedure:
        elements.append(Spacer(1, 0.6*cm))
        elements.append(Paragraph("Procédure expérimentale (IA)", styles["Heading2"]))
        elements.append(Spacer(1, 0.2*cm))
        for line in procedure.strip().split("\n"):
            if line.strip():
                elements.append(Paragraph(
                    line.replace("&", "&amp;").replace("<", "&lt;"), styles["Normal"]))
            else:
                elements.append(Spacer(1, 0.15*cm))
    doc.build(elements)
    return buf.getvalue()

# =============================================================================
# IA — prompts & streaming  (mêmes textes que stochio_qt.py)
# =============================================================================
_SYSTEM_PROCEDURE = (
    "Tu es un chimiste expert en synthèse organique avec une maîtrise rigoureuse des mécanismes réactionnels. "
    "Avant de prédire le produit d'une réaction, tu DOIS :\n"
    "1. Identifier TOUS les groupements fonctionnels de chaque réactif.\n"
    "2. Appliquer l'ordre de réactivité correct basé sur la nucléophilie et la sélectivité connue.\n"
    "3. Déterminer quel groupement réagit EN PREMIER selon cet ordre.\n\n"
    "Règles de réactivité importantes :\n"
    "- Avec un ISOCYANATE (R-N=C=O) : ordre = amine primaire > amine secondaire > thiol (SH) >> eau > alcool > acide carboxylique. "
    "Un thiol avec un isocyanate donne un S-THIOCARBAMATE (liaison C(=O)-S), PAS un O-thiocarbamate ni un amide.\n"
    "- Avec un ÉPOXYDE : les thiols et amines sont plus réactifs que les alcools.\n"
    "- Avec un ANHYDRIDE : les amines réagissent avant les alcools.\n\n"
    "Règles de nomenclature IUPAC :\n"
    "- Le nom IUPAC doit être EN ANGLAIS. Exemples : 'mercaptoacetic' et non 'mercaptoacétique'.\n"
    "- Vérifier la cohérence entre la structure et le nom généré.\n\n"
    "RIGUEUR MÉCANIQUE OBLIGATOIRE avant tout nommage :\n"
    "1. Compter les carbones de chaque réactif.\n"
    "2. Identifier si le mécanisme est Addition, Élimination ou Substitution.\n"
    "3. Calculer le bilan carbone explicite.\n"
    "4. Le nom IUPAC généré DOIT correspondre exactement à ce bilan."
)

_SYSTEM_CHAT = (
    "Tu es un chimiste expert en synthèse organique. "
    "Tu assistes l'utilisateur en répondant à ses questions sur une procédure de synthèse. "
    "Sois concis, pratique et précis. Réponds en français."
)

def _build_prompt(reagents, prod, conditions):
    lim = next((r for r in reagents if r["role"] == "Limitant"), None)
    n_lim_mol = None
    if lim and lim["mw"] and lim["mass_g"]:
        n_lim_mol = lim["mass_g"] * (lim["purity"] / 100.0) / lim["mw"]

    lignes = []
    for r in reagents:
        role = r["role"]; nom = r["name"]; mw = r["mw"]
        pur = r["purity"]; eq = r.get("eq", 1.0)
        mass = r["mass_g"]; dens = r.get("density", 0.0)
        if role == "Limitant":
            mol = n_lim_mol
            masse_txt = f"{mass:.3f} g" if mass else ""
        else:
            mol = eq * n_lim_mol if n_lim_mol else None
            mc = mol * mw / (pur / 100.0) if (mol and mw and pur > 0) else None
            masse_txt = (f"{mc:.3f} g ({mc/dens:.2f} mL)" if (dens and mc) else
                         f"{mc:.3f} g" if mc else "")
        mol_txt = f"{mol*1000:.2f} mmol" if mol else ""
        eq_txt  = f", {eq:.2f} eq" if role != "Limitant" else ", 1 eq (limitant)"
        pur_txt = f", pureté {pur:.0f}%" if pur < 100 else ""
        mw_txt  = f", MW={mw:.2f} g/mol" if mw else ""
        ligne   = f"- [{role}] {nom}{mw_txt}{pur_txt}{eq_txt}"
        if masse_txt: ligne += f" -> {masse_txt}"
        if mol_txt:   ligne += f" ({mol_txt})"
        lignes.append(ligne)

    prod_mw = prod.get("mw") or 0.0
    prod_mw_txt = f"MW estimée du produit : {prod_mw:.2f} g/mol\n" if prod_mw else ""
    cond_parts = []
    if conditions.get("solvant"): cond_parts.append(f"Solvant : {conditions['solvant']}")
    if conditions.get("temp"):    cond_parts.append(f"Température : {conditions['temp']} °C")
    if conditions.get("time"):    cond_parts.append(f"Durée : {conditions['time']} h")
    cond_txt = ("Conditions renseignées : " + ", ".join(cond_parts) + "\n") if cond_parts else ""

    return (
        "Voici les réactifs d'une réaction :\n"
        + "\n".join(lignes) + "\n"
        + prod_mw_txt + cond_txt +
        """
Génère une fiche de synthèse structurée EN FRANÇAIS, en respectant EXACTEMENT ce format (5 sections) :

1. INTRODUCTION
Type de réaction : [type précis]
Groupements fonctionnels impliqués : [lister les GF de chaque réactif, indiquer lequel réagit avec lequel]
Bilan carbone : [ex: C19 + C2 - 0 perte = C21 produit]
Nom potentiel du produit : [nom IUPAC ou courant CORRECT]
Conditions typiques pour ce type de réaction : [solvant usuel, base/acide, température]

2. SUGGESTIONS
- Catalyseur recommandé : [si applicable, sinon "aucun"]
- Solvant recommandé : [solvant + volume approximatif]
- Atmosphère : [air / argon / azote]
- Analyse des équivalents : [cohérence des équivalents]
- Réactifs potentiellement manquants : [liste]
- Autres conseils : [remarques pratiques]

3. PROCEDURE
-. [Peser X mg de réactif A dans un ballon sec de Y mL]
-. [Ajouter Z mL de solvant puis le réactif B sous agitation]
-. [Chauffer à XX °C pendant X h sous reflux]
-. Suivi par CCM (éluant recommandé : [éluant])

4. PURIFICATION
-. Quencher avec [solution appropriée]
-. Extraire avec [solvant], X fois
-. Laver la phase organique avec [lavages]
-. Sécher sur [agent desséchant], filtrer, évaporer sous pression réduite
-. Purifier par [colonne / recristallisation / distillation] : [conditions]

5. PRODUIT ATTENDU
Nom IUPAC : [nom IUPAC EN ANGLAIS, complet et rigoureux]
Nom courant : [nom usuel si applicable, sinon "aucun"]
Masse molaire : [X] g/mol
Rendement typique estimé : [X-X] %
Remarques : [état physique, couleur, stabilité, conservation]

Ne génère RIEN d'autre que ces 5 sections numérotées. Sois concis et pratique."""
    )

def _gemini_gen(prompt, system_msg, api_key):
    if not _GEMINI_OK:
        yield "Erreur : google-genai non installé (pip install google-generativeai)"; return
    try:
        client = genai_lib.Client(api_key=api_key)
        stream = client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_lib.types.GenerateContentConfig(
                system_instruction=system_msg,
                thinking_config=genai_lib.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        for chunk in stream:
            try:
                text = chunk.text or ""
            except Exception:
                text = ""
            if text:
                yield text
    except Exception as e:
        yield f"\nErreur : {e}"

def _groq_gen(prompt, system_msg, api_key, max_tokens=1024):
    if not _GROQ_OK:
        yield "Erreur : groq non installé (pip install groq)"; return
    try:
        client = groq_lib.Groq(api_key=api_key)
        stream = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system_msg},
                      {"role": "user",   "content": prompt}],
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text:
                yield text
    except Exception as e:
        yield f"\nErreur : {e}"

def _gemini_chat_gen(messages, system_msg, api_key):
    if not _GEMINI_OK:
        yield "Erreur : google-genai non installé"; return
    try:
        client = genai_lib.Client(api_key=api_key)
        stream = client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=messages,
            config=genai_lib.types.GenerateContentConfig(
                system_instruction=system_msg,
                thinking_config=genai_lib.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        for chunk in stream:
            try:
                text = chunk.text or ""
            except Exception:
                text = ""
            if text:
                yield text
    except Exception as e:
        yield f"\nErreur : {e}"

def _groq_chat_gen(messages_history, context, system_msg, api_key):
    if not _GROQ_OK:
        yield "Erreur : groq non installé"; return
    try:
        client = groq_lib.Groq(api_key=api_key)
        msgs = [{"role": "system", "content": system_msg + "\n\n" + context}]
        for m in messages_history:
            msgs.append({"role": m["role"], "content": m["content"]})
        stream = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=512,
            messages=msgs,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text:
                yield text
    except Exception as e:
        yield f"\nErreur : {e}"

def extract_ai_info(txt):
    info = {}
    m = re.search(r"^Nom IUPAC\s*:\s*(.+)$", txt, re.MULTILINE)
    if not m:
        m = re.search(r"^Nom courant\s*:\s*(.+)$", txt, re.MULTILINE)
    if m:
        info["prod_name"] = m.group(1).strip()

    m = re.search(r"Masse molaire\s*:\s*([\d][0-9.,]*)\s*g/mol", txt, re.IGNORECASE)
    if m:
        try: info["prod_mw"] = float(m.group(1).replace(",", "."))
        except ValueError: pass

    m = re.search(r"Rendement typique estim[eé]\s*:\s*([\d.]+)\s*[-–àa]\s*([\d.]+)\s*%", txt, re.IGNORECASE)
    if m:
        try: info["prod_yield"] = max(float(m.group(1)), float(m.group(2))) / 100.0
        except ValueError: pass
    else:
        m = re.search(r"Rendement typique estim[eé]\s*:\s*([\d.]+)\s*%", txt, re.IGNORECASE)
        if m:
            try: info["prod_yield"] = float(m.group(1)) / 100.0
            except ValueError: pass

    m = re.search(r"Solvant[s]?\s*(?:recommand[eé]s?)?\s*:\s*([^\n\.]+)", txt, re.IGNORECASE)
    if m: info["solvant"] = m.group(1).strip().rstrip(",")

    m = re.search(r"Temp[eé]rature\s*(?:[\w\s]*):\s*([\d]+)\s*°?\s*C\b", txt, re.IGNORECASE)
    if m:
        info["temp"] = m.group(1).strip()
    else:
        m = re.search(r"\b[àa]\s*([\d]+)\s*[-–]\s*([\d]+)\s*°\s*C\b", txt, re.IGNORECASE)
        if m: info["temp"] = str(max(int(m.group(1)), int(m.group(2))))
        else:
            m = re.search(r"\b[àa]\s*([\d]+)\s*°\s*C\b", txt, re.IGNORECASE)
            if m: info["temp"] = m.group(1).strip()

    m = re.search(r"(?:Dur[eé]e|Temps)\s*(?:[\w\s]*):\s*([\d.,]+)\s*h\b", txt, re.IGNORECASE)
    if m:
        info["time"] = m.group(1).replace(",", ".").strip()
    else:
        m = re.search(r"pendant\s+([\d]+)\s*[-–]\s*([\d]+)\s*h(?:eure)?s?\b", txt, re.IGNORECASE)
        if m: info["time"] = str(max(int(m.group(1)), int(m.group(2))))
        else:
            m = re.search(r"pendant\s+([\d.,]+)\s*h(?:eure)?s?\b", txt, re.IGNORECASE)
            if m: info["time"] = m.group(1).replace(",", ".").strip()

    return info

# =============================================================================
# Streamlit UI
# =============================================================================
st.set_page_config(
    page_title="Stœchiométrie H&B",
    page_icon="⚗️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------- Session state (avant CSS pour utiliser les couleurs) ----------
def _init():
    defaults = {
        "reagents": [],
        "prod": {"name": "", "mw": 0.0, "mw_manual": False,
                 "yield": 1.0, "yield_manual": False,
                 "mass": 0.0, "mass_manual": False, "density": 0.0},
        "conditions": {"solvant": "", "temp": "", "time": ""},
        "rxn_name": "Synthèse",
        "procedure": "",
        "chat_history": [],
        "_pc_prefill": None,
        # ── Thème global ──
        "th_bg":          "#f1f5f9",
        "th_text":        "#0f172a",
        "th_font_size":   14,
        # ── Info box (résultat en haut) ──
        "th_ib_bg":       "#1e40af",
        "th_ib_text":     "#ffffff",
        "th_ib_fs":       16,
        # ── Onglets ──
        "th_tab_bar":     "#e2e8f0",
        "th_tab_text":    "#334155",
        "th_tab_act_bg":  "#1e40af",
        "th_tab_act_txt": "#ffffff",
        # ── Cartes réactifs ──
        "th_card_bg":     "#ffffff",
        "th_card_name_fs": 15,
        "th_card_info_fs": 13,
        # ── Bordures par rôle ──
        "th_bord_lim":    "#2563eb",
        "th_bord_reac":   "#16a34a",
        "th_bord_solv":   "#d97706",
        "th_bord_cat":    "#9333ea",
        "th_bord_aut":    "#64748b",
        # ── Badges par rôle ──
        "th_badge_lim":   "#2563eb",
        "th_badge_reac":  "#16a34a",
        "th_badge_solv":  "#d97706",
        "th_badge_cat":   "#9333ea",
        "th_badge_aut":   "#64748b",
        # ── Champs de saisie ──
        "th_inp_bg":      "#ffffff",
        "th_inp_text":    "#0f172a",
        "th_inp_border":  "#94a3b8",
        "th_inp_fs":      14,
        # ── Formulaire ajout réactif ──
        "th_form_bg":     "#ffffff",
        "th_form_border": "#e2e8f0",
        "th_form_lbl":    "#0f172a",
        "th_form_lbl_fs": 13,
        # ── Selectbox / menus déroulants ──
        "th_sel_bg":      "#ffffff",
        "th_sel_text":    "#0f172a",
        "th_sel_border":  "#94a3b8",
        # ── Expanders ──
        "th_exp_bg":      "#ffffff",
        "th_exp_border":  "#e2e8f0",
        "th_exp_title":   "#0f172a",
        # ── Boutons ──
        "th_btn_bg":      "#1e40af",
        "th_btn_text":    "#ffffff",
        "th_btn_fs":      14,
        # ── Layout paramètres ──
        "cfg_layout":     "Menus déroulants",
        # ── Titres / Labels ──
        "lbl_titre":      "⚗️ Stœchiométrie H&B",
        "lbl_tab_r":      "🧪 Réactifs",
        "lbl_tab_t":      "📊 Tableau",
        "lbl_tab_ia":     "🤖 IA",
        "lbl_tab_ex":     "📤 Export",
        "lbl_tab_cfg":    "⚙️ Paramètres",
        "lbl_add":        "Ajouter un réactif",
        "lbl_inv":        "📦 Depuis mon inventaire",
        "lbl_list":       "Réactifs ajoutés",
        "lbl_cond":       "Conditions réactionnelles",
        "lbl_produit":    "Produit",
        "lbl_resultats":  "📊 Résultats",
        "lbl_ia":         "Procédure IA",
        "lbl_export":     "Exporter",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ---------- CSS dynamique ----------
_th = st.session_state
st.markdown(f"""
<style>
/* ── Fond & texte global ── */
[data-testid="stAppViewContainer"] {{ background: {_th.th_bg}; }}
[data-testid="stMain"] {{ padding: 1rem 0.75rem 2rem; }}
p, span, div, label, small, .stMarkdown, .stText,
[data-testid="stWidgetLabel"] > div,
[data-testid="stCaptionContainer"] {{
    color: {_th.th_text} !important;
    font-size: {_th.th_font_size}px;
}}
h1, h2, h3, h4 {{ color: {_th.th_text} !important; font-weight: 700 !important; }}

/* ── Onglets ── */
.stTabs [data-baseweb="tab-list"] {{
    background: {_th.th_tab_bar}; border-radius: 8px; padding: 4px;
}}
.stTabs [data-baseweb="tab"] {{
    padding: 8px 16px; font-weight: 700; font-size: {_th.th_font_size}px;
    border-radius: 6px; color: {_th.th_tab_text} !important; background: transparent;
}}
.stTabs [aria-selected="true"] {{
    background: {_th.th_tab_act_bg} !important;
    color: {_th.th_tab_act_txt} !important;
}}

/* ── Info box résultat ── */
.info-box {{
    background: {_th.th_ib_bg}; border-radius: 10px;
    padding: 14px 18px; margin: 8px 0;
    color: {_th.th_ib_text} !important;
    font-size: {_th.th_ib_fs}px; font-weight: 700;
}}

/* ── Cartes réactifs ── */
.rcard {{
    background: {_th.th_card_bg}; border-radius: 10px;
    border-left: 5px solid {_th.th_bord_aut};
    padding: 10px 14px; margin-bottom: 6px;
}}
.rcard.lim  {{ border-left-color: {_th.th_bord_lim}; }}
.rcard.reac {{ border-left-color: {_th.th_bord_reac}; }}
.rcard.solv {{ border-left-color: {_th.th_bord_solv}; }}
.rcard.cat  {{ border-left-color: {_th.th_bord_cat}; }}
.rcard .rname {{
    font-size: {_th.th_card_name_fs}px; font-weight: 700;
    color: {_th.th_text} !important;
}}
.rcard .rinfo {{
    font-size: {_th.th_card_info_fs}px; color: {_th.th_text} !important; margin-top: 2px;
}}

/* ── Badges rôle ── */
.role-badge {{
    display: inline-block; border-radius: 6px; padding: 1px 8px;
    font-size: 11px; font-weight: 700; margin-left: 8px; color: #ffffff !important;
}}
.badge-lim  {{ background: {_th.th_badge_lim}; }}
.badge-reac {{ background: {_th.th_badge_reac}; }}
.badge-solv {{ background: {_th.th_badge_solv}; }}
.badge-cat  {{ background: {_th.th_badge_cat}; }}
.badge-aut  {{ background: {_th.th_badge_aut}; }}

/* ── Inputs ── */
input, textarea {{
    background: {_th.th_inp_bg} !important;
    color: {_th.th_inp_text} !important;
    font-size: {_th.th_inp_fs}px !important;
    border-color: {_th.th_inp_border} !important;
}}
[data-baseweb="input"], [data-baseweb="textarea"] {{
    background: {_th.th_inp_bg} !important;
    border-color: {_th.th_inp_border} !important;
}}

/* ── Boutons ── */
.stButton > button {{
    background: {_th.th_btn_bg} !important;
    color: {_th.th_btn_text} !important;
    font-weight: 700; border-radius: 8px;
    font-size: {_th.th_btn_fs}px; padding: 8px 14px;
    border: none !important;
}}

/* ── Formulaire ajout réactif ── */
[data-testid="stForm"] {{
    background: {_th.th_form_bg} !important;
    border: 1px solid {_th.th_form_border} !important;
    border-radius: 10px; padding: 12px;
}}
[data-testid="stForm"] label,
[data-testid="stForm"] [data-testid="stWidgetLabel"] > div {{
    color: {_th.th_form_lbl} !important;
    font-size: {_th.th_form_lbl_fs}px !important;
    font-weight: 600;
}}

/* ── Selectbox / menus déroulants ── */
[data-baseweb="select"] > div,
[data-baseweb="select"] [data-baseweb="input"] {{
    background: {_th.th_sel_bg} !important;
    border-color: {_th.th_sel_border} !important;
}}
[data-baseweb="select"] span,
[data-baseweb="select"] [data-baseweb="input"] input {{
    color: {_th.th_sel_text} !important;
}}
/* Menu déroulant ouvert */
[data-baseweb="popover"] [data-baseweb="menu"] {{
    background: {_th.th_sel_bg} !important;
}}
[data-baseweb="popover"] [role="option"] {{
    color: {_th.th_sel_text} !important;
}}

/* ── Expanders ── */
[data-testid="stExpander"] {{
    background: {_th.th_exp_bg} !important;
    border: 1px solid {_th.th_exp_border} !important;
    border-radius: 10px;
}}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary p {{
    color: {_th.th_exp_title} !important;
    font-weight: 700;
}}

/* ── Tableau ── */
.dataframe td, .dataframe th {{
    color: {_th.th_text} !important; font-size: {_th.th_font_size - 1}px;
}}
</style>
""", unsafe_allow_html=True)

# ---------- Header ----------
h1, h2 = st.columns([3, 2])
with h1:
    st.markdown(f"## {st.session_state.lbl_titre}")
with h2:
    rxn = st.text_input("Réaction", value=st.session_state.rxn_name,
                         label_visibility="collapsed", placeholder="Nom de la réaction…",
                         key="_rxn_name_widget")
    st.session_state.rxn_name = rxn

# ---------- Recalcul global ----------
n_lim, results, prod_result = recalc(st.session_state.reagents, st.session_state.prod)

if n_lim:
    prod_mass_mg = prod_result.get("mass_g", 0) * 1000
    st.markdown(
        f'<div class="info-box">⚖️ n(limitant) = <b>{n_lim:.6f} mol</b> &nbsp;•&nbsp; '
        f'Produit théorique = <b>{prod_mass_mg:.3f} mg</b></div>',
        unsafe_allow_html=True
    )

st.divider()

# ── TABS ──────────────────────────────────────────────────────────────────────
_s = st.session_state
tab_r, tab_t, tab_ia, tab_ex, tab_cfg = st.tabs(
    [_s.lbl_tab_r, _s.lbl_tab_t, _s.lbl_tab_ia, _s.lbl_tab_ex, _s.lbl_tab_cfg]
)

# ===========================================================================
# TAB 1 — Réactifs
# ===========================================================================
with tab_r:

    # ── PubChem ──────────────────────────────────────────────────────────────
    with st.expander("🔍 Rechercher via PubChem", expanded=False):
        pc1, pc2 = st.columns([3, 1])
        with pc1:
            pc_q = st.text_input("Nom IUPAC ou CAS", key="_pc_query", placeholder="ex : acide acétique")
        with pc2:
            st.write("")
            pc_go = st.button("Chercher", width="stretch")
        if pc_go and pc_q:
            with st.spinner("Recherche PubChem…"):
                pc_res, pc_err = pc_search(pc_q)
            if pc_err:
                st.error(pc_err)
            else:
                st.success(f"✅ {pc_res['name']} — MW : {pc_res['mw']} g/mol — {pc_res['formula']}")
                st.session_state._pc_prefill = pc_res

    # ── Formulaire ajout ─────────────────────────────────────────────────────
    st.subheader(st.session_state.lbl_add)
    inv = load_inventaire()
    if inv:
        noms_inv = [p["nom"] for p in inv]
        noms_uniq = list(dict.fromkeys(noms_inv))
        sel_inv = st.selectbox(st.session_state.lbl_inv, noms_uniq,
                               index=None, placeholder="Taper pour rechercher…", key="_inv_sel")
        if sel_inv:
            match = next((p for p in inv if p["nom"] == sel_inv), None)
            if match:
                st.session_state._pc_prefill = {"name": match["nom"], "mw": match["mw"] or 0}
        elif sel_inv is None and st.session_state.get("_inv_sel_was_set"):
            st.session_state._pc_prefill = None
        st.session_state["_inv_sel_was_set"] = sel_inv is not None
    prefill = st.session_state._pc_prefill or {}

    with st.form("form_add", clear_on_submit=True):
        already_lim = any(r["role"] == "Limitant" for r in st.session_state.reagents)
        c1, c2, c3, c4 = st.columns([3, 1.5, 1.5, 2])
        with c1:
            f_name = st.text_input("Nom *", value=prefill.get("name", ""))
        with c2:
            f_role = st.selectbox("Rôle", ROLES, index=1 if already_lim else 0)
        with c3:
            f_mw = st.number_input("MW (g/mol) *", value=float(prefill.get("mw") or 0),
                                    min_value=0.0, step=0.01, format="%.4f")
        with c4:
            masse_label = "Masse (g) *" if f_role == "Limitant" else "Équivalents *"
            masse_step  = 0.001 if f_role == "Limitant" else 0.1
            masse_def   = 0.0   if f_role == "Limitant" else 1.0
            f_val_str   = st.text_input(masse_label, value="",
                                        placeholder="ex : 0.250" if f_role == "Limitant" else "ex : 1.0")

        cp1, cp2 = st.columns(2)
        with cp1:
            f_purity = st.number_input("Pureté (%)", value=100.0,
                                        min_value=0.1, max_value=100.0, step=0.1)
        with cp2:
            f_density = st.number_input("Densité (g/mL) — optionnel", value=0.0,
                                         min_value=0.0, step=0.001, format="%.4f")

        add_btn = st.form_submit_button("➕ Ajouter le réactif", width="stretch")

        try:
            f_val = float(f_val_str.replace(",", ".")) if f_val_str.strip() else 0.0
        except ValueError:
            f_val = 0.0

        if add_btn:
            err = None
            if not f_name.strip():
                err = "Le nom est obligatoire."
            elif f_mw <= 0:
                err = "MW invalide (doit être > 0)."
            elif f_role == "Limitant" and already_lim:
                err = "Un réactif Limitant existe déjà."
            elif f_role == "Limitant" and f_val <= 0:
                err = "La masse est obligatoire pour le Limitant."
            elif f_role != "Limitant" and f_val <= 0:
                err = "Les équivalents doivent être > 0."
            if err:
                st.error(err)
            else:
                st.session_state.reagents.append({
                    "name":    f_name.strip(),
                    "mw":      f_mw,
                    "purity":  f_purity,
                    "role":    f_role,
                    "eq":      1.0 if f_role == "Limitant" else f_val,
                    "mass_g":  f_val if f_role == "Limitant" else 0.0,
                    "density": f_density,
                })
                st.session_state._pc_prefill = None
                st.session_state.prod["mw_manual"] = False
                st.session_state.prod["mw"] = 0.0
                st.rerun()

    # ── Liste des réactifs ───────────────────────────────────────────────────
    if st.session_state.reagents:
        st.subheader(st.session_state.lbl_list)

        ROLE_CSS = {
            "Limitant":   ("lim",  "badge-lim"),
            "Réactif":    ("reac", "badge-reac"),
            "Solvant":    ("solv", "badge-solv"),
            "Catalyseur": ("cat",  "badge-cat"),
            "Autre":      ("",     "badge-aut"),
        }

        to_delete = None
        for i, r in enumerate(st.session_state.reagents):
            card_cls, badge_cls = ROLE_CSS.get(r["role"], ("", "badge-aut"))
            dens_txt = f" | d={r['density']}" if r.get("density") else ""
            val_txt  = f"Masse : {r['mass_g']} g" if r["role"] == "Limitant" else f"Éq : {r['eq']}"

            col_info, col_edit, col_del = st.columns([3, 2, 1])
            with col_info:
                st.markdown(
                    f'<div class="rcard {card_cls}">'
                    f'<div class="rname">{r["name"]}'
                    f'<span class="role-badge {badge_cls}">{r["role"]}</span></div>'
                    f'<div class="rinfo">MW : {r["mw"]} g/mol | {val_txt} | Pureté : {r["purity"]}%{dens_txt}</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
            with col_edit:
                if r["role"] == "Limitant":
                    new_v = st.number_input(
                        "Masse (g)", value=float(r["mass_g"]),
                        min_value=0.0, step=0.001, format="%.4f",
                        key=f"_edit_mass_{i}", label_visibility="collapsed"
                    )
                    st.session_state.reagents[i]["mass_g"] = new_v
                else:
                    new_v = st.number_input(
                        "Éq", value=float(r["eq"]),
                        min_value=0.0, step=0.1, format="%.3f",
                        key=f"_edit_eq_{i}", label_visibility="collapsed"
                    )
                    st.session_state.reagents[i]["eq"] = new_v
            with col_del:
                st.write("")
                if st.button("🗑️", key=f"_del_{i}"):
                    to_delete = i

        if to_delete is not None:
            st.session_state.reagents.pop(to_delete)
            st.session_state.prod["mw_manual"] = False
            st.session_state.prod["mw"] = 0.0
            st.rerun()

        if st.button("🗑️ Réinitialiser tout", type="secondary"):
            st.session_state.reagents = []
            st.session_state.prod = {
                "name": "", "mw": 0.0, "mw_manual": False,
                "yield": 1.0, "yield_manual": False,
                "mass": 0.0, "mass_manual": False, "density": 0.0
            }
            st.session_state.conditions = {"solvant": "", "temp": "", "time": ""}
            st.session_state.procedure = ""
            st.session_state.chat_history = []
            st.rerun()

    else:
        st.info("Ajoutez un réactif **Limitant** pour commencer.")

    # ── Conditions & Produit ─────────────────────────────────────────────────
    if st.session_state.reagents:
        st.subheader(st.session_state.lbl_cond)
        cc1, cc2, cc3 = st.columns(3)
        cond = st.session_state.conditions
        with cc1:
            opts = [""] + SOLVANTS_USUELS
            idx = opts.index(cond["solvant"]) if cond["solvant"] in opts else 0
            sel = st.selectbox("Solvant", opts, index=idx, key="_cond_solvant")
            st.session_state.conditions["solvant"] = sel
        with cc2:
            t = st.text_input("T (°C)", value=cond["temp"], placeholder="ex : 80", key="_cond_temp")
            st.session_state.conditions["temp"] = t
        with cc3:
            d = st.text_input("t (h)", value=cond["time"], placeholder="ex : 2", key="_cond_time")
            st.session_state.conditions["time"] = d

        st.subheader(st.session_state.lbl_produit)
        prod = st.session_state.prod
        pp1, pp2, pp3, pp4 = st.columns(4)
        with pp1:
            pn = st.text_input("Nom produit", value=prod["name"], key="_prod_name")
            st.session_state.prod["name"] = pn
        with pp2:
            pmw = st.number_input("MW (g/mol)", value=float(prod["mw"] or 0),
                                   min_value=0.0, step=0.01, key="_prod_mw")
            if pmw != prod["mw"]:
                st.session_state.prod["mw"] = pmw
                st.session_state.prod["mw_manual"] = pmw > 0
        with pp3:
            pyd = st.number_input("Rendement (0–1)", value=float(prod["yield"]),
                                   min_value=0.0, max_value=1.0, step=0.05, key="_prod_yield")
            if pyd != prod["yield"]:
                st.session_state.prod["yield"] = pyd
                st.session_state.prod["yield_manual"] = True
                st.session_state.prod["mass_manual"] = False
        with pp4:
            pdens = st.number_input("Densité produit", value=float(prod["density"] or 0),
                                     min_value=0.0, step=0.001, format="%.4f", key="_prod_dens")
            st.session_state.prod["density"] = pdens

# ===========================================================================
# TAB 2 — Tableau
# ===========================================================================
with tab_t:
    st.subheader(st.session_state.lbl_resultats)
    if not st.session_state.reagents:
        st.info("Ajoutez des réactifs dans l'onglet Réactifs pour voir le tableau.")
    else:
        n_lim2, results2, prod_result2 = recalc(st.session_state.reagents, st.session_state.prod)
        prod_name = st.session_state.prod["name"] or "Produit"
        df = build_display_df(st.session_state.reagents, results2, prod_result2, prod_name)

        # Style : colonne produit en vert, autres en bleu très clair
        def _style(df):
            styles = pd.DataFrame("", index=df.index, columns=df.columns)
            for col in df.columns:
                if col == prod_name:
                    styles[col] = "background-color:#f0fdf4; color:#15803d; font-weight:bold"
                else:
                    styles[col] = "background-color:#f8fafc"
            return styles

        st.dataframe(df.style.apply(_style, axis=None), width="stretch")

        # Conditions
        cond = st.session_state.conditions
        parts = []
        if cond["solvant"]: parts.append(f"Solvant : **{cond['solvant']}**")
        if cond["temp"]:    parts.append(f"T : **{cond['temp']} °C**")
        if cond["time"]:    parts.append(f"t : **{cond['time']} h**")
        if parts:
            st.markdown("**Conditions :** " + "   |   ".join(parts))

# ===========================================================================
# TAB 3 — IA
# ===========================================================================
with tab_ia:
    if not st.session_state.reagents:
        st.info("Ajoutez des réactifs d'abord.")
    else:
        col_proc, col_chat = st.columns([1, 1])

        # ── Procédure ────────────────────────────────────────────────────────
        with col_proc:
            st.subheader("Procédure expérimentale")

            if st.button("🤖 Générer la procédure", type="primary", width="stretch"):
                lim_ok = any(r["role"] == "Limitant" for r in st.session_state.reagents)
                if not lim_ok:
                    st.error("Définissez un réactif Limitant d'abord.")
                else:
                    provider = charger_provider()
                    api_key  = charger_api_key(provider)
                    if not api_key:
                        st.error("Configurez votre clé API dans l'onglet **Paramètres**.")
                    else:
                        prompt = _build_prompt(
                            st.session_state.reagents,
                            st.session_state.prod,
                            st.session_state.conditions
                        )
                        if provider == "gemini":
                            gen = _gemini_gen(prompt, _SYSTEM_PROCEDURE, api_key)
                        else:
                            gen = _groq_gen(prompt, _SYSTEM_PROCEDURE, api_key, max_tokens=1024)

                        full = st.write_stream(gen)
                        st.session_state.procedure = full

                        # Extraction automatique
                        info = extract_ai_info(full)
                        p = st.session_state.prod
                        if "prod_name" in info and not p["name"]:
                            st.session_state.prod["name"] = info["prod_name"]
                        if "prod_mw" in info and not p["mw_manual"]:
                            st.session_state.prod["mw"] = info["prod_mw"]
                            st.session_state.prod["mw_manual"] = True
                        if "prod_yield" in info and not p["yield_manual"]:
                            st.session_state.prod["yield"] = info["prod_yield"]
                            st.session_state.prod["yield_manual"] = True
                        c = st.session_state.conditions
                        if "solvant" in info and not c["solvant"]:
                            st.session_state.conditions["solvant"] = info["solvant"]
                        if "temp" in info and not c["temp"]:
                            st.session_state.conditions["temp"] = info["temp"]
                        if "time" in info and not c["time"]:
                            st.session_state.conditions["time"] = info["time"]
                        st.rerun()

            if st.session_state.procedure:
                st.text_area("Procédure", value=st.session_state.procedure,
                             height=420, label_visibility="collapsed", key="_proc_display")
                if st.button("🗑️ Effacer"):
                    st.session_state.procedure = ""
                    st.rerun()

        # ── Chat ─────────────────────────────────────────────────────────────
        with col_chat:
            st.subheader("Chat IA")
            if not st.session_state.procedure:
                st.info("Générez d'abord une procédure pour activer le chat.")
            else:
                for msg in st.session_state.chat_history:
                    with st.chat_message(msg["role"]):
                        st.write(msg["content"])

                user_q = st.chat_input("Posez une question sur la procédure…")
                if user_q:
                    provider = charger_provider()
                    api_key  = charger_api_key(provider)
                    if not api_key:
                        st.error("Clé API manquante.")
                    else:
                        st.session_state.chat_history.append({"role": "user", "content": user_q})
                        context = (
                            "Voici la fiche de synthèse :\n\n"
                            + st.session_state.procedure
                            + "\n\nRéponds en te basant sur cette fiche. Sois concis."
                        )
                        with st.chat_message("assistant"):
                            if provider == "gemini":
                                msgs_g = [
                                    {"role": "user",  "parts": [{"text": context}]},
                                    {"role": "model", "parts": [{"text": "Compris."}]},
                                ]
                                for m in st.session_state.chat_history[:-1]:
                                    role = "user" if m["role"] == "user" else "model"
                                    msgs_g.append({"role": role, "parts": [{"text": m["content"]}]})
                                msgs_g.append({"role": "user", "parts": [{"text": st.session_state.chat_history[-1]["content"]}]})
                                response = st.write_stream(
                                    _gemini_chat_gen(msgs_g, _SYSTEM_CHAT, api_key)
                                )
                            else:
                                response = st.write_stream(
                                    _groq_chat_gen(st.session_state.chat_history,
                                                   context, _SYSTEM_CHAT, api_key)
                                )
                        st.session_state.chat_history.append({"role": "assistant", "content": response})

                if st.session_state.chat_history:
                    if st.button("🗑️ Effacer le chat"):
                        st.session_state.chat_history = []
                        st.rerun()

# ===========================================================================
# TAB 4 — Export
# ===========================================================================
with tab_ex:
    if not st.session_state.reagents:
        st.info("Ajoutez des réactifs d'abord.")
    else:
        _, res_ex, pr_ex = recalc(st.session_state.reagents, st.session_state.prod)
        prod_name_ex = st.session_state.prod["name"] or "Produit"
        df_ex = build_display_df(st.session_state.reagents, res_ex, pr_ex, prod_name_ex)

        col_names  = list(df_ex.columns)
        row_labels = list(df_ex.index)
        matrix     = [[df_ex.at[r, c] or "-" for c in col_names] for r in row_labels]

        st.subheader("Télécharger")
        ex1, ex2 = st.columns(2)

        with ex1:
            csv_lines = [";" + ";".join(col_names)]
            for label, row in zip(row_labels, matrix):
                csv_lines.append(label + ";" + ";".join(row))
            if st.session_state.procedure:
                csv_lines += ["", "", "Procédure expérimentale (IA)", st.session_state.procedure]
            st.download_button(
                "📄 Télécharger CSV",
                data="\n".join(csv_lines).encode("utf-8-sig"),
                file_name=f"{st.session_state.rxn_name}_stochio.csv",
                mime="text/csv",
                width="stretch",
            )

        with ex2:
            try:
                pdf_bytes = make_pdf(col_names, row_labels, matrix,
                                     st.session_state.rxn_name,
                                     procedure=st.session_state.procedure)
                st.download_button(
                    "📑 Télécharger PDF",
                    data=pdf_bytes,
                    file_name=f"{st.session_state.rxn_name}_stochio.pdf",
                    mime="application/pdf",
                    width="stretch",
                )
            except Exception as e:
                st.error(f"Erreur PDF : {e}")

# ===========================================================================
# TAB 5 — Paramètres
# ===========================================================================
def _cp(label, key):
    """Color picker qui applique immédiatement."""
    v = st.color_picker(label, value=st.session_state[key], key=f"_cp_{key}")
    if v != st.session_state[key]:
        st.session_state[key] = v
        st.rerun()

def _sl(label, key, lo, hi):
    """Slider entier qui applique immédiatement."""
    v = st.slider(label, min_value=lo, max_value=hi,
                  value=st.session_state[key], step=1, key=f"_sl_{key}")
    if v != st.session_state[key]:
        st.session_state[key] = v
        st.rerun()

def _ti(label, key):
    """Text input pour renommer un titre."""
    v = st.text_input(label, value=st.session_state[key], key=f"_ti_{key}")
    if v != st.session_state[key]:
        st.session_state[key] = v
        st.rerun()

_ALL_TH_KEYS = [
    "th_bg","th_text","th_font_size",
    "th_ib_bg","th_ib_text","th_ib_fs",
    "th_tab_bar","th_tab_text","th_tab_act_bg","th_tab_act_txt",
    "th_inp_bg","th_inp_text","th_inp_border","th_inp_fs",
    "th_form_bg","th_form_border","th_form_lbl","th_form_lbl_fs",
    "th_sel_bg","th_sel_text","th_sel_border",
    "th_exp_bg","th_exp_border","th_exp_title",
    "th_card_bg","th_card_name_fs","th_card_info_fs",
    "th_bord_lim","th_bord_reac","th_bord_solv","th_bord_cat","th_bord_aut",
    "th_badge_lim","th_badge_reac","th_badge_solv","th_badge_cat","th_badge_aut",
    "th_btn_bg","th_btn_text","th_btn_fs",
    "lbl_titre","lbl_tab_r","lbl_tab_t","lbl_tab_ia","lbl_tab_ex","lbl_tab_cfg",
    "lbl_add","lbl_inv","lbl_list","lbl_cond","lbl_produit",
    "lbl_resultats","lbl_ia","lbl_export",
]

# ── Contenu de chaque section paramètres (fonctions réutilisables) ──────────
def _cfg_general():
    st.markdown("### 🌍 Page & navigation")
    g1, g2 = st.columns(2)
    with g1:
        _cp("Fond de la page", "th_bg")
        _cp("Couleur du texte global", "th_text")
        _sl("Taille police globale (px)", "th_font_size", 10, 24)
    with g2:
        st.markdown("**Onglets**")
        _cp("Barre des onglets", "th_tab_bar")
        _cp("Texte onglets inactifs", "th_tab_text")
        _cp("Fond onglet actif", "th_tab_act_bg")
        _cp("Texte onglet actif", "th_tab_act_txt")
    st.markdown("### ✏️ Champs de saisie")
    i1, i2 = st.columns(2)
    with i1:
        _cp("Fond des champs", "th_inp_bg")
        _cp("Texte des champs", "th_inp_text")
    with i2:
        _cp("Bordure des champs", "th_inp_border")
        _sl("Taille police champs (px)", "th_inp_fs", 10, 22)
    st.markdown("### 🔘 Boutons")
    b1, b2 = st.columns(2)
    with b1:
        _cp("Fond des boutons", "th_btn_bg")
        _cp("Texte des boutons", "th_btn_text")
    with b2:
        _sl("Taille police boutons (px)", "th_btn_fs", 10, 22)
    st.markdown("### 📦 Boîte résultat (n limitant)")
    r1, r2 = st.columns(2)
    with r1:
        _cp("Fond", "th_ib_bg")
        _cp("Texte", "th_ib_text")
    with r2:
        _sl("Taille police (px)", "th_ib_fs", 10, 24)
    st.markdown("### 🏷️ Noms des onglets")
    n1, n2 = st.columns(2)
    with n1:
        _ti("Titre de l'application", "lbl_titre")
        _ti("Onglet Réactifs", "lbl_tab_r")
        _ti("Onglet Tableau", "lbl_tab_t")
    with n2:
        _ti("Onglet IA", "lbl_tab_ia")
        _ti("Onglet Export", "lbl_tab_ex")
        _ti("Onglet Paramètres", "lbl_tab_cfg")

def _cfg_reactifs():
    st.markdown("### ✏️ Titres de la section")
    t1, t2 = st.columns(2)
    with t1:
        _ti("Titre 'Ajouter un réactif'", "lbl_add")
        _ti("Label inventaire", "lbl_inv")
        _ti("Titre liste réactifs", "lbl_list")
    with t2:
        _ti("Titre conditions", "lbl_cond")
        _ti("Titre produit", "lbl_produit")
    st.markdown("### 📋 Formulaire d'ajout")
    f1, f2 = st.columns(2)
    with f1:
        _cp("Fond du formulaire", "th_form_bg")
        _cp("Bordure du formulaire", "th_form_border")
    with f2:
        _cp("Couleur des labels", "th_form_lbl")
        _sl("Taille police labels (px)", "th_form_lbl_fs", 9, 20)

    st.markdown("### 🔽 Boîtes déroulantes (inventaire, rôle…)")
    s1, s2 = st.columns(2)
    with s1:
        _cp("Fond de la boîte", "th_sel_bg")
        _cp("Texte", "th_sel_text")
    with s2:
        _cp("Bordure", "th_sel_border")

    st.markdown("### 📂 Sections dépliables (expanders)")
    e1, e2 = st.columns(2)
    with e1:
        _cp("Fond", "th_exp_bg")
        _cp("Bordure", "th_exp_border")
    with e2:
        _cp("Couleur du titre", "th_exp_title")
    st.markdown("### 🃏 Cartes réactifs ajoutés")
    c1, c2 = st.columns(2)
    with c1:
        _cp("Fond de la carte", "th_card_bg")
        _sl("Police nom composé (px)", "th_card_name_fs", 10, 24)
        _sl("Police infos MW/masse (px)", "th_card_info_fs", 9, 20)
    with c2:
        st.markdown("**Bordure gauche par rôle**")
        _cp("Limitant", "th_bord_lim")
        _cp("Réactif", "th_bord_reac")
        _cp("Solvant", "th_bord_solv")
        _cp("Catalyseur", "th_bord_cat")
        _cp("Autre", "th_bord_aut")
    st.markdown("### 🏷️ Badges de rôle")
    bd1, bd2 = st.columns(2)
    with bd1:
        _cp("Limitant", "th_badge_lim")
        _cp("Réactif", "th_badge_reac")
        _cp("Solvant", "th_badge_solv")
    with bd2:
        _cp("Catalyseur", "th_badge_cat")
        _cp("Autre", "th_badge_aut")

def _cfg_tableau():
    st.markdown("### ✏️ Titres")
    _ti("Titre de la section résultats", "lbl_resultats")
    _ti("Titre section Export", "lbl_export")

def _cfg_ia():
    st.markdown("### ✏️ Titres")
    _ti("Titre section IA", "lbl_ia")
    st.divider()
    provider_actif = charger_provider()
    key_ok = bool(charger_api_key(provider_actif))
    if key_ok:
        st.success(f"✅ Fournisseur : **{provider_actif}** — clé OK")
    else:
        st.error("❌ Clé API manquante dans `.streamlit/secrets.toml`")

_SECTIONS = {
    "🌐 Général":    _cfg_general,
    "🧪 Réactifs":   _cfg_reactifs,
    "📊 Tableau":    _cfg_tableau,
    "🤖 IA":         _cfg_ia,
}

with tab_cfg:

    # ── Choix du mode d'affichage ─────────────────────────────────────────────
    mode_col, _ = st.columns([2, 3])
    with mode_col:
        mode = st.selectbox(
            "Mode d'affichage des paramètres",
            ["Menus déroulants", "Accordéons"],
            index=0 if st.session_state.cfg_layout == "Menus déroulants" else 1,
            key="_cfg_layout_sel",
        )
        if mode != st.session_state.cfg_layout:
            st.session_state.cfg_layout = mode
            st.rerun()

    st.divider()

    if st.session_state.cfg_layout == "Menus déroulants":
        # Un selectbox choisit quelle section afficher
        sec = st.selectbox("Section", list(_SECTIONS.keys()), key="_cfg_sec")
        st.markdown("---")
        _SECTIONS[sec]()
    else:
        # Accordéons (expanders)
        for titre, fn in _SECTIONS.items():
            with st.expander(titre):
                fn()

    # ── Reset global ──────────────────────────────────────────────────────────
    st.divider()
    if st.button("↩️ Tout réinitialiser"):
        for k in _ALL_TH_KEYS:
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()
    st.markdown("*Stœchiométrie H&B · Streamlit + Gemini / Groq + PubChem*")
