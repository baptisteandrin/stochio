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
# Streamlit UI  — redesign mobile-first
# =============================================================================
st.set_page_config(
    page_title="Stœchiométrie H&B",
    page_icon="⚗️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
/* ── Base ── */
[data-testid="stAppViewContainer"] { background: #f8fafc; }
[data-testid="stMain"] { padding: 1rem 1rem 2rem; }

/* ── Info box ── */
.info-box {
    background: linear-gradient(135deg,#1e40af,#2563eb);
    border-radius: 12px; padding: 12px 18px; margin: 8px 0;
    color: #fff; font-size: 15px; font-weight: 500;
}

/* ── Section card ── */
.section-card {
    background: #fff; border-radius: 14px; border: 1px solid #e2e8f0;
    padding: 16px; margin-bottom: 14px;
}

/* ── Résultats table ── */
.result-table { font-size: 13px; }

/* ── Boutons mobiles ── */
@media (max-width: 768px) {
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap; gap: 6px; }
    .stButton > button { font-size: 15px; padding: 10px 12px; border-radius: 10px; }
    [data-testid="stDataEditor"] { font-size: 13px; }
}

/* ── Expanders ── */
[data-testid="stExpander"] {
    background: #fff; border-radius: 12px !important;
    border: 1px solid #e2e8f0 !important; margin-bottom: 10px;
}
[data-testid="stExpander"] summary { font-weight: 700; font-size: 15px; }
</style>
""", unsafe_allow_html=True)

# ---------- Session state ----------
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
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ---------- Helper : reagents ↔ DataFrame ----------
def _reagents_to_df(reagents):
    if not reagents:
        return pd.DataFrame([{
            "Nom": "", "Rôle": "Limitant", "MW (g/mol)": None,
            "Masse (g)": None, "Éq": None, "Pureté (%)": 100.0, "Densité": None,
        }])
    rows = []
    for r in reagents:
        rows.append({
            "Nom":        r["name"],
            "Rôle":       r["role"],
            "MW (g/mol)": float(r["mw"]) if r["mw"] else None,
            "Masse (g)":  float(r["mass_g"]) if r["role"] == "Limitant" and r["mass_g"] else None,
            "Éq":         float(r["eq"]) if r["role"] != "Limitant" and r["eq"] else None,
            "Pureté (%)": float(r["purity"] or 100),
            "Densité":    float(r["density"]) if r.get("density") else None,
        })
    return pd.DataFrame(rows)

def _df_to_reagents(df):
    reagents = []
    for _, row in df.iterrows():
        nom = str(row.get("Nom") or "").strip()
        if not nom:
            continue
        role = str(row.get("Rôle") or "Réactif")
        mw   = float(row["MW (g/mol)"]) if pd.notna(row.get("MW (g/mol)")) and row.get("MW (g/mol)") else 0.0
        mass = float(row["Masse (g)"])  if pd.notna(row.get("Masse (g)"))  and row.get("Masse (g)")  else 0.0
        eq   = float(row["Éq"])         if pd.notna(row.get("Éq"))         and row.get("Éq")         else 1.0
        pur  = float(row["Pureté (%)"])  if pd.notna(row.get("Pureté (%)")) and row.get("Pureté (%)") else 100.0
        dens = float(row["Densité"])    if pd.notna(row.get("Densité"))    and row.get("Densité")     else 0.0
        reagents.append({
            "name": nom, "role": role, "mw": mw,
            "mass_g": mass if role == "Limitant" else 0.0,
            "eq":     1.0  if role == "Limitant" else eq,
            "purity": pur, "density": dens,
        })
    return reagents

# ── Header ────────────────────────────────────────────────────────────────────
h1, h2 = st.columns([2, 3])
with h1:
    st.markdown("## ⚗️ Stœchiométrie")
with h2:
    rxn = st.text_input("Réaction", value=st.session_state.rxn_name,
                         label_visibility="collapsed", placeholder="Nom de la réaction…",
                         key="_rxn_name_widget")
    st.session_state.rxn_name = rxn

# ── Recalcul ─────────────────────────────────────────────────────────────────
n_lim, results, prod_result = recalc(st.session_state.reagents, st.session_state.prod)
if n_lim:
    prod_mass_mg = prod_result.get("mass_g", 0) * 1000
    st.markdown(
        f'<div class="info-box">⚖️ n(limitant) = <b>{n_lim*1000:.4f} mmol</b>'
        f'&nbsp;&nbsp;·&nbsp;&nbsp;Produit théorique = <b>{prod_mass_mg:.2f} mg</b></div>',
        unsafe_allow_html=True,
    )

st.divider()

# ===========================================================================
# SECTION 1 — Tableau de saisie (data_editor)
# ===========================================================================
inv      = load_inventaire()
inv_dict = {p["nom"].lower(): p for p in inv} if inv else {}

# Auto-remplir MW depuis inventaire quand un nom est connu et MW manquante
_updated = False
for i, r in enumerate(st.session_state.reagents):
    if r["name"] and not r["mw"]:
        match = inv_dict.get(r["name"].lower())
        if match and match.get("mw"):
            st.session_state.reagents[i]["mw"] = match["mw"]
            _updated = True
if _updated:
    # Forcer le data_editor à se reconstruire depuis session_state
    if "_reagents_editor" in st.session_state:
        del st.session_state["_reagents_editor"]
    st.rerun()

col_title, col_reset = st.columns([4, 1])
with col_title:
    st.subheader("Réactifs")
with col_reset:
    if st.button("🗑️ Reset", help="Réinitialiser tout"):
        st.session_state.reagents  = []
        st.session_state.prod      = {"name":"","mw":0.0,"mw_manual":False,"yield":1.0,
                                       "yield_manual":False,"mass":0.0,"mass_manual":False,"density":0.0}
        st.session_state.conditions = {"solvant":"","temp":"","time":""}
        st.session_state.procedure  = ""
        st.session_state.chat_history = []
        st.rerun()

df_in  = _reagents_to_df(st.session_state.reagents)
edited = st.data_editor(
    df_in,
    column_config={
        "Nom":        st.column_config.TextColumn("Nom du composé", width="large"),
        "Rôle":       st.column_config.SelectboxColumn("Rôle", options=ROLES, width="medium"),
        "MW (g/mol)": st.column_config.NumberColumn("MW (g/mol)", min_value=0, format="%.2f"),
        "Masse (g)":  st.column_config.NumberColumn("Masse (g)", min_value=0, format="%.4f",
                                                     help="Remplir uniquement pour le Limitant"),
        "Éq":         st.column_config.NumberColumn("Éq", min_value=0, format="%.3f",
                                                     help="Remplir pour les autres réactifs"),
        "Pureté (%)": st.column_config.NumberColumn("Pureté %", min_value=0, max_value=100, format="%.1f"),
        "Densité":    st.column_config.NumberColumn("Densité", min_value=0, format="%.3f"),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="_reagents_editor",
)

new_reagents = _df_to_reagents(edited)
if new_reagents != st.session_state.reagents:
    st.session_state.reagents = new_reagents
    st.session_state.prod["mw_manual"] = False
    st.session_state.prod["mw"] = 0.0

# Recherche PubChem
with st.expander("🔍 Rechercher une MW via PubChem", expanded=False):
    pc1, pc2 = st.columns([4, 1])
    with pc1:
        pc_q = st.text_input("Nom IUPAC ou CAS", key="_pc_query", placeholder="ex : acide acétique",
                              label_visibility="collapsed")
    with pc2:
        pc_go = st.button("Chercher", width="stretch")
    if pc_go and pc_q:
        with st.spinner("Recherche…"):
            pc_res, pc_err = pc_search(pc_q)
        if pc_err:
            st.error(pc_err)
        else:
            st.success(f"**{pc_res['name']}** — MW : **{pc_res['mw']} g/mol** ({pc_res['formula']})")

st.divider()

# ===========================================================================
# SECTION 2 — Tableau des résultats calculés
# ===========================================================================
n_lim2, results2, prod_result2 = recalc(st.session_state.reagents, st.session_state.prod)
if n_lim2 and results2:
    prod_name = st.session_state.prod["name"] or "Produit"
    df_res = build_display_df(st.session_state.reagents, results2, prod_result2, prod_name)

    # Transposé : réactifs en lignes, propriétés en colonnes
    df_t = df_res.T

    def _style_res(df):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        for row in df.index:
            if row == prod_name:
                styles.loc[row] = "background-color:#dcfce7; color:#15803d; font-weight:bold"
            else:
                styles.loc[row] = "background-color:#f8fafc; color:#1e293b"
        return styles

    st.subheader("📊 Résultats")
    st.dataframe(df_t.style.apply(_style_res, axis=None), use_container_width=True)

    cond  = st.session_state.conditions
    parts = []
    if cond["solvant"]: parts.append(f"Solvant : **{cond['solvant']}**")
    if cond["temp"]:    parts.append(f"T : **{cond['temp']} °C**")
    if cond["time"]:    parts.append(f"t : **{cond['time']} h**")
    if parts:
        st.caption("Conditions : " + "   ·   ".join(parts))

elif st.session_state.reagents:
    st.info("Renseignez le **Rôle = Limitant**, sa **MW** et sa **Masse (g)** pour calculer.")

st.divider()

# ===========================================================================
# SECTION 3 — Produit & Conditions
# ===========================================================================
with st.expander("🧪 Produit & Conditions", expanded=False):
    prod = st.session_state.prod
    pa, pb, pc_, pd_ = st.columns(4)
    with pa:
        pn = st.text_input("Nom produit", value=prod["name"], key="_prod_name")
        st.session_state.prod["name"] = pn
    with pb:
        pmw = st.number_input("MW produit (g/mol)", value=float(prod["mw"] or 0),
                               min_value=0.0, step=0.01, key="_prod_mw")
        if pmw != prod["mw"]:
            st.session_state.prod["mw"] = pmw
            st.session_state.prod["mw_manual"] = pmw > 0
    with pc_:
        pyd = st.number_input("Rendement (0–1)", value=float(prod["yield"]),
                               min_value=0.0, max_value=1.0, step=0.05, key="_prod_yield")
        if pyd != prod["yield"]:
            st.session_state.prod["yield"] = pyd
            st.session_state.prod["yield_manual"] = True
            st.session_state.prod["mass_manual"]  = False
    with pd_:
        pdens = st.number_input("Densité produit", value=float(prod["density"] or 0),
                                 min_value=0.0, step=0.001, format="%.4f", key="_prod_dens")
        st.session_state.prod["density"] = pdens

    st.caption("Conditions réactionnelles")
    ca, cb, cc = st.columns(3)
    cond = st.session_state.conditions
    with ca:
        opts = [""] + SOLVANTS_USUELS
        idx  = opts.index(cond["solvant"]) if cond["solvant"] in opts else 0
        sel  = st.selectbox("Solvant", opts, index=idx, key="_cond_solvant")
        st.session_state.conditions["solvant"] = sel
    with cb:
        t = st.text_input("T (°C)", value=cond["temp"], placeholder="ex : 80", key="_cond_temp")
        st.session_state.conditions["temp"] = t
    with cc:
        d = st.text_input("t (h)", value=cond["time"], placeholder="ex : 2", key="_cond_time")
        st.session_state.conditions["time"] = d

# ===========================================================================
# SECTION 4 — IA
# ===========================================================================
with st.expander("🤖 Procédure IA & Chat", expanded=False):
    if not st.session_state.reagents:
        st.info("Ajoutez des réactifs pour activer l'IA.")
    else:
        ia1, ia2 = st.columns([1, 1])

        with ia1:
            st.markdown("**Procédure expérimentale**")
            if st.button("🤖 Générer", type="primary", width="stretch", key="_ia_gen"):
                lim_ok = any(r["role"] == "Limitant" for r in st.session_state.reagents)
                if not lim_ok:
                    st.error("Définissez un réactif Limitant.")
                else:
                    provider = charger_provider()
                    api_key  = charger_api_key(provider)
                    if not api_key:
                        st.error("Clé API manquante (Paramètres).")
                    else:
                        prompt = _build_prompt(st.session_state.reagents,
                                               st.session_state.prod,
                                               st.session_state.conditions)
                        gen = (_gemini_gen(prompt, _SYSTEM_PROCEDURE, api_key)
                               if provider == "gemini"
                               else _groq_gen(prompt, _SYSTEM_PROCEDURE, api_key, max_tokens=1024))
                        full = st.write_stream(gen)
                        st.session_state.procedure = full
                        info = extract_ai_info(full)
                        p = st.session_state.prod
                        if "prod_name"  in info and not p["name"]:         st.session_state.prod["name"]  = info["prod_name"]
                        if "prod_mw"    in info and not p["mw_manual"]:    st.session_state.prod["mw"]    = info["prod_mw"];  st.session_state.prod["mw_manual"]    = True
                        if "prod_yield" in info and not p["yield_manual"]: st.session_state.prod["yield"] = info["prod_yield"]; st.session_state.prod["yield_manual"] = True
                        c = st.session_state.conditions
                        if "solvant" in info and not c["solvant"]: st.session_state.conditions["solvant"] = info["solvant"]
                        if "temp"    in info and not c["temp"]:    st.session_state.conditions["temp"]    = info["temp"]
                        if "time"    in info and not c["time"]:    st.session_state.conditions["time"]    = info["time"]
                        st.rerun()

            if st.session_state.procedure:
                st.text_area("Procédure", value=st.session_state.procedure,
                             height=380, label_visibility="collapsed", key="_proc_display")
                if st.button("🗑️ Effacer procédure", key="_proc_clear"):
                    st.session_state.procedure = ""
                    st.rerun()

        with ia2:
            st.markdown("**Chat**")
            if not st.session_state.procedure:
                st.info("Générez une procédure pour activer le chat.")
            else:
                for msg in st.session_state.chat_history:
                    with st.chat_message(msg["role"]):
                        st.write(msg["content"])
                user_q = st.chat_input("Question sur la procédure…")
                if user_q:
                    provider = charger_provider()
                    api_key  = charger_api_key(provider)
                    if not api_key:
                        st.error("Clé API manquante.")
                    else:
                        st.session_state.chat_history.append({"role": "user", "content": user_q})
                        context = ("Fiche de synthèse :\n\n" + st.session_state.procedure
                                   + "\n\nRéponds en te basant sur cette fiche. Sois concis.")
                        with st.chat_message("assistant"):
                            if provider == "gemini":
                                msgs_g = [{"role":"user","parts":[{"text":context}]},
                                           {"role":"model","parts":[{"text":"Compris."}]}]
                                for m in st.session_state.chat_history[:-1]:
                                    msgs_g.append({"role":"user" if m["role"]=="user" else "model",
                                                   "parts":[{"text":m["content"]}]})
                                msgs_g.append({"role":"user","parts":[{"text":st.session_state.chat_history[-1]["content"]}]})
                                response = st.write_stream(_gemini_chat_gen(msgs_g, _SYSTEM_CHAT, api_key))
                            else:
                                response = st.write_stream(_groq_chat_gen(
                                    st.session_state.chat_history, context, _SYSTEM_CHAT, api_key))
                        st.session_state.chat_history.append({"role":"assistant","content":response})
                if st.session_state.chat_history:
                    if st.button("🗑️ Effacer chat", key="_chat_clear"):
                        st.session_state.chat_history = []
                        st.rerun()

# ===========================================================================
# SECTION 5 — Export
# ===========================================================================
with st.expander("📤 Export CSV / PDF", expanded=False):
    if not st.session_state.reagents:
        st.info("Ajoutez des réactifs d'abord.")
    else:
        _, res_ex, pr_ex = recalc(st.session_state.reagents, st.session_state.prod)
        prod_name_ex = st.session_state.prod["name"] or "Produit"
        df_ex = build_display_df(st.session_state.reagents, res_ex, pr_ex, prod_name_ex)
        col_names  = list(df_ex.columns)
        row_labels = list(df_ex.index)
        matrix     = [[df_ex.at[r, c] or "-" for c in col_names] for r in row_labels]

        ex1, ex2 = st.columns(2)
        with ex1:
            csv_lines = [";" + ";".join(col_names)]
            for label, row in zip(row_labels, matrix):
                csv_lines.append(label + ";" + ";".join(row))
            if st.session_state.procedure:
                csv_lines += ["", "", "Procédure expérimentale (IA)", st.session_state.procedure]
            st.download_button("📄 CSV", data="\n".join(csv_lines).encode("utf-8-sig"),
                               file_name=f"{st.session_state.rxn_name}_stochio.csv",
                               mime="text/csv", width="stretch")
        with ex2:
            try:
                pdf_bytes = make_pdf(col_names, row_labels, matrix,
                                     st.session_state.rxn_name, procedure=st.session_state.procedure)
                st.download_button("📑 PDF", data=pdf_bytes,
                                   file_name=f"{st.session_state.rxn_name}_stochio.pdf",
                                   mime="application/pdf", width="stretch")
            except Exception as e:
                st.error(f"Erreur PDF : {e}")

# ===========================================================================
# SECTION 6 — Paramètres
# ===========================================================================
with st.expander("⚙️ Paramètres", expanded=False):
    provider_actif = charger_provider()
    key_ok = bool(charger_api_key(provider_actif))
    if key_ok:
        st.success(f"✅ Fournisseur actif : **{provider_actif}** — clé configurée")
    else:
        st.error("❌ Aucune clé API trouvée dans `.streamlit/secrets.toml`")
    st.caption("**À propos** — Stœchiométrie H&B · Streamlit + Gemini 2.5 Flash / Groq + PubChem")
