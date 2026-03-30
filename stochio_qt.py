#!/usr/bin/env python3
"""
Calculateur de Stochiometrie - PyQt6
pip install PyQt6 pandas pubchempy reportlab groq openpyxl rdkit google-generativeai
python stochio_qt.py
"""

import io
import json
import re
import threading
from pathlib import Path

import pandas as pd
import pubchempy as pcp
import groq as groq_lib
try:
    from google import genai as genai_lib
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib import colors as rl_colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QComboBox, QSplitter,
    QScrollArea, QTextEdit, QGroupBox, QFrame, QFileDialog, QMessageBox,
    QDialog, QCompleter, QSizePolicy, QFontComboBox, QSpinBox,
    QColorDialog, QTabWidget, QFormLayout, QCheckBox,
)
from PyQt6.QtCore import Qt, QStringListModel, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QTextCharFormat, QAction, QTextCursor, QColor

# =============================================================================
# Chemins & configuration
# =============================================================================
_BASE_DIR = Path(__file__).parent
INVENTAIRE_PATH = _BASE_DIR / "Inventaire.xlsx"
PUBCHEM_DB_PATH = _BASE_DIR / "DataBasePubMeb.xlsx"
CONFIG_PATH = Path.home() / ".stochio_config.json"


def charger_pubchem_db() -> list:
    """Charge la base PubChem locale (générée par scrape_pubchem.py)."""
    if not PUBCHEM_DB_PATH.exists():
        return []
    try:
        df = pd.read_excel(PUBCHEM_DB_PATH, dtype=str)
        resultats = []
        for _, row in df.iterrows():
            nom = str(row.get("Nom", "") or "").strip()
            mw_raw = str(row.get("MW (g/mol)", "") or "").strip()
            if nom and nom.lower() != "nan":
                try:
                    mw = float(mw_raw.replace(",", ".")) if mw_raw and mw_raw.lower() != "nan" else None
                except ValueError:
                    mw = None
                resultats.append({"nom": nom, "mw": mw})
        return resultats
    except Exception:
        return []


def charger_inventaire() -> list:
    try:
        df = pd.read_excel(INVENTAIRE_PATH, dtype=str)
        resultats = []
        for _, row in df.iterrows():
            nom = str(row.get("Nom du Produit", "") or "").strip()
            mw_raw = str(row.get("Masse Molaire (g/mol)", "") or "").strip()
            if nom and nom.lower() != "nan":
                try:
                    mw = float(mw_raw.replace(",", ".")) if mw_raw and mw_raw.lower() != "nan" else None
                except ValueError:
                    mw = None
                resultats.append({"nom": nom, "mw": mw})
        return resultats
    except Exception:
        return []


def _charger_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sauvegarder_config(**kwargs):
    cfg = _charger_config()
    cfg.update(kwargs)
    CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")


def charger_provider() -> str:
    return _charger_config().get("ai_provider", "gemini")


def charger_api_key(provider: str | None = None) -> str:
    cfg = _charger_config()
    if provider is None:
        provider = cfg.get("ai_provider", "gemini")
    field = "gemini_key" if provider == "gemini" else "groq_key"
    return cfg.get(field, "")


# =============================================================================
# Utilitaires
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


def _wrap_name(name: str) -> str:
    """Insère des opportunités de coupure de ligne invisibles pour les noms IUPAC.
    Qt ne coupe que sur les espaces par défaut ; U+200B (zero-width space) indique
    où une coupure est autorisée sans modifier le texte affiché."""
    ZWS = "\u200b"
    return (name
        .replace("-", "-" + ZWS)   # après chaque tiret IUPAC
        .replace("(", ZWS + "(")   # avant chaque parenthèse ouvrante
        .replace(")", ")" + ZWS)   # après chaque parenthèse fermante
        .replace(",", "," + ZWS)   # après chaque virgule
    )


# =============================================================================
# Export PDF
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
    elements = [Paragraph(f"Stochiometrie — {rxn_name}", styles["Title"]),
                Spacer(1, 0.5*cm), t]
    if procedure:
        elements.append(Spacer(1, 0.6*cm))
        elements.append(Paragraph("Procedure experimentale (IA)", styles["Heading2"]))
        elements.append(Spacer(1, 0.2*cm))
        for line in procedure.strip().split("\n"):
            if line.strip():
                elements.append(Paragraph(
                    line.replace("&", "&amp;").replace("<", "&lt;"), styles["Normal"]
                ))
            else:
                elements.append(Spacer(1, 0.15*cm))
    doc.build(elements)
    return buf.getvalue()


# =============================================================================
# Constantes
# =============================================================================
ROWS = [
    ("MW (g/mol)",     "mw",      True,  True,  True),
    ("Masse (g)",      "mass_g",  True,  False, True),
    ("n (mol)",        "mol",     False, False, False),
    ("Eq",             "eq",      False, True,  True),
    ("Densite (g/mL)", "density", True,  True,  True),
    ("Volume (mL)",    "volume",  False, False, False),
    ("Purete (%)",     "purity",  True,  True,  False),
]

SOLVANTS_USUELS = [
    "Eau", "Methanol", "Ethanol", "Isopropanol", "Acide acetique",
    "n-Butanol", "Acetone", "Acetonitrile", "Dimethylformamide (DMF)",
    "Dimethylsulfoxyde (DMSO)", "Tetrahydrofurane (THF)", "Dichloromethane (DCM)",
    "Acetate d'ethyle", "Ether diethylique", "Toluene",
    "Hexane", "Cyclohexane", "Pentane", "Chloroforme", "Xylene",
]

COL_W  = 160
ROW_H  = 46
HDR_H  = 58
PROP_W = 155

# Palette
BG       = "#ffffff"
PANEL    = "#f8fafc"
BORDER   = "#e2e8f0"
ACCENT   = "#2563eb"
TEXT     = "#1e293b"
DIM      = "#94a3b8"
GREEN    = "#16a34a"
RED      = "#dc2626"
C_INPUT  = "#ffffff"
C_CALC   = "#f1f5f9"
C_PROD   = "#f0fdf4"
C_PROD_C = "#dcfce7"
C_HEAD   = "#dbeafe"
C_HEAD_L = "#eff6ff"
C_LABEL  = "#f8fafc"
C_COND   = "#fefce8"
BORDER_IN = "#93c5fd"


# =============================================================================
# Feuille de style globale
# =============================================================================
# Paramètres d'affichage
# =============================================================================
DEFAULT_DISPLAY_SETTINGS = {
    # ── Formulaire ──────────────────────────────────────────────────────────
    "font_family":        "Segoe UI",
    "font_size":          11,
    "font_bold":          False, "font_italic": False, "font_underline": False,
    "text_color":         "#1e293b",
    "field_w_name":       160,
    "field_w_mw":         100,
    "field_w_mass":       100,
    "field_w_pur":        70,
    "field_h":            32,
    "label_font_family":  "Segoe UI",
    "label_font_size":    9,
    "label_font_bold":    True, "label_font_italic": False, "label_font_underline": False,
    "label_color":        "#64748b",
    # ── Tableau – dimensions ─────────────────────────────────────────────────
    "tbl_col_w":          160,
    "tbl_row_h":          46,
    "tbl_hdr_h":          58,
    "tbl_prop_w":         155,
    # ── Tableau – étiquettes de ligne (MW, Masse, n, Eq …) ──────────────────
    "tbl_lbl_font_family": "Segoe UI",
    "tbl_lbl_font_size":   10,
    "tbl_lbl_bold":        True, "tbl_lbl_italic": False, "tbl_lbl_underline": False,
    "tbl_lbl_color":       "#2563eb",
    "tbl_lbl_bg":          "#f8fafc",
    # ── Tableau – en-têtes colonnes (noms des réactifs) ──────────────────────
    "tbl_hdr_font_family": "Segoe UI",
    "tbl_hdr_font_size":   8,
    "tbl_hdr_bold":        True, "tbl_hdr_italic": False, "tbl_hdr_underline": False,
    "tbl_hdr_color":       "#2563eb",
    "tbl_hdr_bg":          "#dbeafe",
    # ── Tableau – cellules valeurs (réactifs) ────────────────────────────────
    "tbl_cell_font_family": "Segoe UI",
    "tbl_cell_font_size":   13,
    "tbl_cell_bold":        False, "tbl_cell_italic": False, "tbl_cell_underline": False,
    "tbl_cell_color":       "#1e293b",
    "tbl_cell_bg_input":    "#ffffff",
    "tbl_cell_bg_calc":     "#f1f5f9",
    # ── Tableau – colonne produit ────────────────────────────────────────────
    "tbl_prod_font_family": "Segoe UI",
    "tbl_prod_font_size":   13,
    "tbl_prod_bold":        False, "tbl_prod_italic": False, "tbl_prod_underline": False,
    "tbl_prod_color":       "#16a34a",
    "tbl_prod_bg":          "#f0fdf4",
    "tbl_prod_bg_calc":     "#dcfce7",
    # ── Tableau – colonne conditions (Solvant, T°C, t(h)) ────────────────────
    "tbl_cond_font_family": "Segoe UI",
    "tbl_cond_font_size":   13,
    "tbl_cond_bold":        False, "tbl_cond_italic": False, "tbl_cond_underline": False,
    "tbl_cond_color":       "#1e293b",
    "tbl_cond_bg":          "#fefce8",
}


class DisplaySettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Affichage")
        self.setMinimumWidth(460)
        self.resize(460, 600)
        self.setStyleSheet(f"background: {BG};")

        # ── Stockage centralisé des couleurs ────────────────────────────────
        d = DEFAULT_DISPLAY_SETTINGS
        self._colors: dict[str, str] = {
            k: settings.get(k, d[k])
            for k in (
                "text_color", "label_color",
                "tbl_lbl_color", "tbl_lbl_bg",
                "tbl_hdr_color", "tbl_hdr_bg",
                "tbl_cell_color", "tbl_cell_bg_input", "tbl_cell_bg_calc",
                "tbl_prod_color", "tbl_prod_bg", "tbl_prod_bg_calc",
                "tbl_cond_color", "tbl_cond_bg",
            )
        }

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {BORDER}; border-radius: 6px; background: white;
            }}
            QTabBar::tab {{
                padding: 6px 14px; background: {PANEL}; color: {DIM};
                border: 1px solid {BORDER}; border-bottom: none;
                border-radius: 4px 4px 0 0; font-size: 11px;
            }}
            QTabBar::tab:selected {{
                background: white; color: {ACCENT}; font-weight: bold;
            }}
        """)

        # ── Helpers locaux ──────────────────────────────────────────────────
        def spinbox(vmin, vmax, val, suffix=" px"):
            sb = QSpinBox()
            sb.setRange(vmin, vmax)
            sb.setValue(val)
            sb.setSuffix(suffix)
            sb.setFixedHeight(30)
            sb.setStyleSheet(f"""
                QSpinBox {{
                    border: 1px solid {BORDER}; border-radius: 4px;
                    padding: 2px 6px; background: white; color: {TEXT};
                }}
                QSpinBox:focus {{ border-color: {ACCENT}; }}
            """)
            return sb

        def form_tab(title):
            """Onglet avec QScrollArea pour gérer les contenus longs."""
            outer = QWidget()
            outer.setStyleSheet("background: white;")
            outer_vl = QVBoxLayout(outer)
            outer_vl.setContentsMargins(0, 0, 0, 0)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("background: white;")
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            inner = QWidget()
            inner.setStyleSheet(f"background: white; color: {TEXT};")
            fl = QFormLayout(inner)
            fl.setSpacing(10)
            fl.setContentsMargins(16, 14, 16, 14)
            fl.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

            scroll.setWidget(inner)
            outer_vl.addWidget(scroll)
            tabs.addTab(outer, title)
            return fl

        def sep_label(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {DIM}; font-size: 10px; font-weight: bold; padding-top: 6px;"
            )
            return lbl

        _chk_ss = f"QCheckBox {{ color: {TEXT}; background: transparent; }} QCheckBox::indicator {{ width: 14px; height: 14px; }}"
        _fcb_ss = (
            f"QFontComboBox {{ color: {TEXT}; background: white;"
            f"  border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 4px; }}"
            f"QFontComboBox QAbstractItemView {{ color: {TEXT}; background: white; }}"
        )

        def style_checks(prefix):
            """Crée un widget avec 3 cases à cocher Gras/Italique/Souligné pour le préfixe donné."""
            w = QWidget(); w.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(w); hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(16)
            cb_b = QCheckBox("Gras");    cb_b.setStyleSheet(_chk_ss)
            cb_b.setChecked(settings.get(f"{prefix}_bold", d.get(f"{prefix}_bold", False)))
            cb_i = QCheckBox("Italique"); cb_i.setStyleSheet(_chk_ss)
            cb_i.setChecked(settings.get(f"{prefix}_italic", d.get(f"{prefix}_italic", False)))
            cb_u = QCheckBox("Souligné"); cb_u.setStyleSheet(_chk_ss)
            cb_u.setChecked(settings.get(f"{prefix}_underline", d.get(f"{prefix}_underline", False)))
            hl.addWidget(cb_b); hl.addWidget(cb_i); hl.addWidget(cb_u); hl.addStretch()
            setattr(self, f"_{prefix}_bold_cb",      cb_b)
            setattr(self, f"_{prefix}_italic_cb",    cb_i)
            setattr(self, f"_{prefix}_underline_cb", cb_u)
            return w

        def font_combo(key):
            cb = QFontComboBox()
            cb.setCurrentFont(QFont(settings.get(key, d[key])))
            cb.setFixedHeight(30)
            cb.setStyleSheet(_fcb_ss)
            return cb

        def color_btn(key, title):
            """Crée un QPushButton coloré lié à self._colors[key]."""
            btn = QPushButton()
            btn.setFixedHeight(30)

            def refresh(color):
                r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
                fg = "white" if (r * 0.299 + g * 0.587 + b * 0.114) < 128 else "#1e293b"
                btn.setText(color)
                btn.setStyleSheet(
                    f"QPushButton {{ background: {color}; color: {fg};"
                    f"border: 1px solid {BORDER}; border-radius: 4px;"
                    f"font-size: 11px; padding: 4px 10px; }}"
                )

            refresh(self._colors[key])

            def pick():
                dlg = QColorDialog(QColor(self._colors[key]), self)
                dlg.setWindowTitle(title)
                dlg.setStyleSheet("")  # reset stylesheet — évite texte invisible hérité
                if dlg.exec():
                    c = dlg.selectedColor()
                    if c.isValid():
                        self._colors[key] = c.name()
                        refresh(c.name())

            btn.clicked.connect(pick)
            return btn

        # ── Onglet Police ───────────────────────────────────────────────────
        fl_font = form_tab("Police")

        self.font_family = QFontComboBox()
        self.font_family.setCurrentFont(QFont(settings["font_family"]))
        self.font_family.setFixedHeight(30)
        self.font_family.setStyleSheet(_fcb_ss)
        fl_font.addRow("Police :", self.font_family)

        self.font_size = spinbox(7, 24, settings["font_size"], " pt")
        fl_font.addRow("Taille :", self.font_size)
        fl_font.addRow("Style :", style_checks("font"))

        self.color_btn = color_btn("text_color", "Couleur du texte")
        fl_font.addRow("Couleur du texte :", self.color_btn)

        # ── Onglet Formulaire ───────────────────────────────────────────────
        fl_form = form_tab("Formulaire")

        self.field_w_name = spinbox(80, 400, settings["field_w_name"])
        fl_form.addRow("Largeur champ Nom :", self.field_w_name)

        self.field_w_mw = spinbox(60, 200, settings["field_w_mw"])
        fl_form.addRow("Largeur champ MW :", self.field_w_mw)

        self.field_w_mass = spinbox(60, 200, settings["field_w_mass"])
        fl_form.addRow("Largeur champ Masse / Eq :", self.field_w_mass)

        self.field_w_pur = spinbox(40, 150, settings["field_w_pur"])
        fl_form.addRow("Largeur champ Purete :", self.field_w_pur)

        self.field_h = spinbox(24, 60, settings["field_h"])
        fl_form.addRow("Hauteur des champs :", self.field_h)

        fl_form.addRow("", sep_label("— Etiquettes (Nom*, MW*, ...) —"))

        self.label_font_family = font_combo("label_font_family")
        fl_form.addRow("Police etiquettes :", self.label_font_family)

        self.label_font_size = spinbox(7, 18, settings.get("label_font_size", d["label_font_size"]), " pt")
        fl_form.addRow("Taille etiquettes :", self.label_font_size)
        fl_form.addRow("Style etiquettes :", style_checks("label_font"))

        self.label_color_btn = color_btn("label_color", "Couleur etiquettes")
        fl_form.addRow("Couleur etiquettes :", self.label_color_btn)

        # ── Onglet Tableau ──────────────────────────────────────────────────
        fl_tbl = form_tab("Tableau")

        fl_tbl.addRow("", sep_label("— Dimensions —"))
        self.tbl_col_w = spinbox(80, 400, settings["tbl_col_w"])
        fl_tbl.addRow("Largeur colonnes :", self.tbl_col_w)
        self.tbl_row_h = spinbox(24, 120, settings["tbl_row_h"])
        fl_tbl.addRow("Hauteur lignes :", self.tbl_row_h)
        self.tbl_hdr_h = spinbox(30, 120, settings["tbl_hdr_h"])
        fl_tbl.addRow("Hauteur en-tetes min. :", self.tbl_hdr_h)
        self.tbl_prop_w = spinbox(80, 300, settings["tbl_prop_w"])
        fl_tbl.addRow("Largeur col. etiquettes :", self.tbl_prop_w)

        fl_tbl.addRow("", sep_label("— Etiquettes de ligne (MW, Masse, n, Eq…) —"))
        self.tbl_lbl_font_family = font_combo("tbl_lbl_font_family")
        fl_tbl.addRow("Police :", self.tbl_lbl_font_family)
        self.tbl_lbl_font_size = spinbox(6, 20, settings.get("tbl_lbl_font_size", d["tbl_lbl_font_size"]), " pt")
        fl_tbl.addRow("Taille :", self.tbl_lbl_font_size)
        fl_tbl.addRow("Style :", style_checks("tbl_lbl"))
        self.tbl_lbl_color_btn = color_btn("tbl_lbl_color", "Couleur texte etiquettes ligne")
        fl_tbl.addRow("Couleur texte :", self.tbl_lbl_color_btn)
        self.tbl_lbl_bg_btn = color_btn("tbl_lbl_bg", "Couleur fond etiquettes ligne")
        fl_tbl.addRow("Couleur fond :", self.tbl_lbl_bg_btn)

        fl_tbl.addRow("", sep_label("— En-tetes colonnes (noms reactifs) —"))
        self.tbl_hdr_font_family = font_combo("tbl_hdr_font_family")
        fl_tbl.addRow("Police :", self.tbl_hdr_font_family)
        self.tbl_hdr_font_size = spinbox(6, 20, settings.get("tbl_hdr_font_size", d["tbl_hdr_font_size"]), " pt")
        fl_tbl.addRow("Taille :", self.tbl_hdr_font_size)
        fl_tbl.addRow("Style :", style_checks("tbl_hdr"))
        self.tbl_hdr_color_btn = color_btn("tbl_hdr_color", "Couleur texte noms reactifs")
        fl_tbl.addRow("Couleur texte :", self.tbl_hdr_color_btn)
        self.tbl_hdr_bg_btn = color_btn("tbl_hdr_bg", "Couleur fond en-tetes")
        fl_tbl.addRow("Couleur fond :", self.tbl_hdr_bg_btn)

        fl_tbl.addRow("", sep_label("— Cellules valeurs (reactifs) —"))
        self.tbl_cell_font_family = font_combo("tbl_cell_font_family")
        fl_tbl.addRow("Police :", self.tbl_cell_font_family)
        self.tbl_cell_font_size = spinbox(6, 24, settings.get("tbl_cell_font_size", d["tbl_cell_font_size"]), " pt")
        fl_tbl.addRow("Taille :", self.tbl_cell_font_size)
        fl_tbl.addRow("Style :", style_checks("tbl_cell"))
        self.tbl_cell_color_btn = color_btn("tbl_cell_color", "Couleur texte cellules")
        fl_tbl.addRow("Couleur texte :", self.tbl_cell_color_btn)
        self.tbl_cell_bg_input_btn = color_btn("tbl_cell_bg_input", "Fond cellules saisie")
        fl_tbl.addRow("Fond saisie :", self.tbl_cell_bg_input_btn)
        self.tbl_cell_bg_calc_btn = color_btn("tbl_cell_bg_calc", "Fond cellules calculees")
        fl_tbl.addRow("Fond calcule :", self.tbl_cell_bg_calc_btn)

        fl_tbl.addRow("", sep_label("— Colonne Produit —"))
        self.tbl_prod_font_family = font_combo("tbl_prod_font_family")
        fl_tbl.addRow("Police :", self.tbl_prod_font_family)
        self.tbl_prod_font_size = spinbox(6, 24, settings.get("tbl_prod_font_size", d["tbl_prod_font_size"]), " pt")
        fl_tbl.addRow("Taille :", self.tbl_prod_font_size)
        fl_tbl.addRow("Style :", style_checks("tbl_prod"))
        self.tbl_prod_color_btn = color_btn("tbl_prod_color", "Couleur texte produit")
        fl_tbl.addRow("Couleur texte :", self.tbl_prod_color_btn)
        self.tbl_prod_bg_btn = color_btn("tbl_prod_bg", "Fond cellules saisie produit")
        fl_tbl.addRow("Fond saisie :", self.tbl_prod_bg_btn)
        self.tbl_prod_bg_calc_btn = color_btn("tbl_prod_bg_calc", "Fond cellules calcul produit")
        fl_tbl.addRow("Fond calcule :", self.tbl_prod_bg_calc_btn)

        fl_tbl.addRow("", sep_label("— Colonne Conditions (Solvant, T, t) —"))
        self.tbl_cond_font_family = font_combo("tbl_cond_font_family")
        fl_tbl.addRow("Police :", self.tbl_cond_font_family)
        self.tbl_cond_font_size = spinbox(6, 24, settings.get("tbl_cond_font_size", d["tbl_cond_font_size"]), " pt")
        fl_tbl.addRow("Taille :", self.tbl_cond_font_size)
        fl_tbl.addRow("Style :", style_checks("tbl_cond"))
        self.tbl_cond_color_btn = color_btn("tbl_cond_color", "Couleur texte conditions")
        fl_tbl.addRow("Couleur texte :", self.tbl_cond_color_btn)
        self.tbl_cond_bg_btn = color_btn("tbl_cond_bg", "Couleur fond conditions")
        fl_tbl.addRow("Couleur fond :", self.tbl_cond_bg_btn)

        layout.addWidget(tabs)

        # ── Boutons ─────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Reinitialiser")
        reset_btn.setFixedHeight(30)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background: {PANEL}; color: {DIM}; border: 1px solid {BORDER};"
            f"border-radius: 6px; padding: 4px 12px; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {TEXT}; }}"
        )
        reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()

        cancel_btn = QPushButton("Annuler")
        cancel_btn.setFixedHeight(30)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};"
            f"border-radius: 6px; padding: 4px 12px; font-size: 11px; }}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        apply_btn = QPushButton("Appliquer")
        apply_btn.setFixedHeight(30)
        apply_btn.setStyleSheet(btn_style())
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    # Propriétés de compatibilité
    @property
    def _color(self):       return self._colors["text_color"]
    @property
    def _label_color(self): return self._colors["label_color"]

    def _reset_defaults(self):
        d = DEFAULT_DISPLAY_SETTINGS
        self.font_family.setCurrentFont(QFont(d["font_family"]))
        self.font_size.setValue(d["font_size"])
        self.field_w_name.setValue(d["field_w_name"])
        self.field_w_mw.setValue(d["field_w_mw"])
        self.field_w_mass.setValue(d["field_w_mass"])
        self.field_w_pur.setValue(d["field_w_pur"])
        self.field_h.setValue(d["field_h"])
        self.label_font_family.setCurrentFont(QFont(d["label_font_family"]))
        self.label_font_size.setValue(d["label_font_size"])
        self.tbl_col_w.setValue(d["tbl_col_w"])
        self.tbl_row_h.setValue(d["tbl_row_h"])
        self.tbl_hdr_h.setValue(d["tbl_hdr_h"])
        self.tbl_prop_w.setValue(d["tbl_prop_w"])
        self.tbl_lbl_font_family.setCurrentFont(QFont(d["tbl_lbl_font_family"]))
        self.tbl_lbl_font_size.setValue(d["tbl_lbl_font_size"])
        self.tbl_hdr_font_family.setCurrentFont(QFont(d["tbl_hdr_font_family"]))
        self.tbl_hdr_font_size.setValue(d["tbl_hdr_font_size"])
        self.tbl_cell_font_family.setCurrentFont(QFont(d["tbl_cell_font_family"]))
        self.tbl_cell_font_size.setValue(d["tbl_cell_font_size"])
        self.tbl_prod_font_family.setCurrentFont(QFont(d["tbl_prod_font_family"]))
        self.tbl_prod_font_size.setValue(d["tbl_prod_font_size"])
        self.tbl_cond_font_family.setCurrentFont(QFont(d["tbl_cond_font_family"]))
        self.tbl_cond_font_size.setValue(d["tbl_cond_font_size"])
        for prefix in ("font", "label_font", "tbl_lbl", "tbl_hdr", "tbl_cell", "tbl_prod", "tbl_cond"):
            getattr(self, f"_{prefix}_bold_cb").setChecked(d.get(f"{prefix}_bold", False))
            getattr(self, f"_{prefix}_italic_cb").setChecked(d.get(f"{prefix}_italic", False))
            getattr(self, f"_{prefix}_underline_cb").setChecked(d.get(f"{prefix}_underline", False))
        for k in self._colors:
            self._colors[k] = d[k]

    def _style_vals(self, prefix) -> dict:
        return {
            f"{prefix}_bold":      getattr(self, f"_{prefix}_bold_cb").isChecked(),
            f"{prefix}_italic":    getattr(self, f"_{prefix}_italic_cb").isChecked(),
            f"{prefix}_underline": getattr(self, f"_{prefix}_underline_cb").isChecked(),
        }

    def get_settings(self) -> dict:
        s = {
            "font_family":           self.font_family.currentFont().family(),
            "font_size":             self.font_size.value(),
            "text_color":            self._colors["text_color"],
            "field_w_name":          self.field_w_name.value(),
            "field_w_mw":            self.field_w_mw.value(),
            "field_w_mass":          self.field_w_mass.value(),
            "field_w_pur":           self.field_w_pur.value(),
            "field_h":               self.field_h.value(),
            "label_font_family":     self.label_font_family.currentFont().family(),
            "label_font_size":       self.label_font_size.value(),
            "label_color":           self._colors["label_color"],
            "tbl_col_w":             self.tbl_col_w.value(),
            "tbl_row_h":             self.tbl_row_h.value(),
            "tbl_hdr_h":             self.tbl_hdr_h.value(),
            "tbl_prop_w":            self.tbl_prop_w.value(),
            "tbl_lbl_font_family":   self.tbl_lbl_font_family.currentFont().family(),
            "tbl_lbl_font_size":     self.tbl_lbl_font_size.value(),
            "tbl_lbl_color":         self._colors["tbl_lbl_color"],
            "tbl_lbl_bg":            self._colors["tbl_lbl_bg"],
            "tbl_hdr_font_family":   self.tbl_hdr_font_family.currentFont().family(),
            "tbl_hdr_font_size":     self.tbl_hdr_font_size.value(),
            "tbl_hdr_color":         self._colors["tbl_hdr_color"],
            "tbl_hdr_bg":            self._colors["tbl_hdr_bg"],
            "tbl_cell_font_family":  self.tbl_cell_font_family.currentFont().family(),
            "tbl_cell_font_size":    self.tbl_cell_font_size.value(),
            "tbl_cell_color":        self._colors["tbl_cell_color"],
            "tbl_cell_bg_input":     self._colors["tbl_cell_bg_input"],
            "tbl_cell_bg_calc":      self._colors["tbl_cell_bg_calc"],
            "tbl_prod_font_family":  self.tbl_prod_font_family.currentFont().family(),
            "tbl_prod_font_size":    self.tbl_prod_font_size.value(),
            "tbl_prod_color":        self._colors["tbl_prod_color"],
            "tbl_prod_bg":           self._colors["tbl_prod_bg"],
            "tbl_prod_bg_calc":      self._colors["tbl_prod_bg_calc"],
            "tbl_cond_font_family":  self.tbl_cond_font_family.currentFont().family(),
            "tbl_cond_font_size":    self.tbl_cond_font_size.value(),
            "tbl_cond_color":        self._colors["tbl_cond_color"],
            "tbl_cond_bg":           self._colors["tbl_cond_bg"],
        }
        for prefix in ("font", "label_font", "tbl_lbl", "tbl_hdr", "tbl_cell", "tbl_prod", "tbl_cond"):
            s.update(self._style_vals(prefix))
        return s


# QCompleter sans filtre préfixe interne (filtrage manuel "contains")
class _ContainsCompleter(QCompleter):
    def splitPath(self, path):
        return [""]          # désactive le filtrage interne → le modèle est déjà filtré

    def pathFromIndex(self, index):
        return index.data()  # retourne le texte brut de l'item


# =============================================================================
APP_QSS = f"""
QMainWindow {{ background: {BG}; }}
QWidget {{ background: {BG}; }}
QMenuBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
    padding: 2px;
    font-size: 12px;
}}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; color: {TEXT}; }}
QMenuBar::item:selected {{ background: {C_HEAD}; color: {ACCENT}; }}
QMenu {{
    background: white;
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 20px; border-radius: 4px; color: {TEXT}; font-size: 12px; }}
QMenu::item:selected {{ background: {C_HEAD}; color: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QStatusBar {{
    background: {PANEL};
    color: {DIM};
    border-top: 1px solid {BORDER};
    font-size: 11px;
    padding: 2px 8px;
}}
QScrollBar:horizontal {{
    height: 8px; background: {PANEL}; border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {DIM}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar:vertical {{
    width: 8px; background: {PANEL}; border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {DIM}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{
    background: {TEXT}; color: white; border: none;
    padding: 4px 8px; border-radius: 4px; font-size: 11px;
}}
"""


def btn_style(bg="#2563eb", hover="#1d4ed8"):
    return f"""
    QPushButton {{
        background: {bg}; color: white; border: none;
        border-radius: 6px; padding: 5px 14px;
        font-weight: bold; font-size: 12px;
    }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:disabled {{ background: {DIM}; }}
    """


def cell_style(bg, fg, border, focus_border=ACCENT):
    return f"""
    QLineEdit {{
        background: {bg}; color: {fg};
        border: 1px solid {border}; border-radius: 0px;
        padding: 0px 4px; font-size: 13px;
    }}
    QLineEdit:focus {{
        border: 2px solid {focus_border};
    }}
    """


# =============================================================================
# Application principale
# =============================================================================
class App(QMainWindow):
    # Signaux thread-safe pour mise a jour UI depuis le thread Groq
    sig_append      = pyqtSignal(str)
    sig_done        = pyqtSignal()
    sig_clear       = pyqtSignal()
    sig_chat_append = pyqtSignal(str)
    sig_chat_done   = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Calculateur de Stochiometrie")
        self.resize(1400, 820)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(APP_QSS)

        # Etat
        self.reagents: list[dict] = []
        inv_local = charger_inventaire()
        inv_pc    = charger_pubchem_db()
        # Fusionner : inventaire local en priorité (évite les doublons)
        noms_locaux = {p["nom"].lower() for p in inv_local}
        self._inventaire = inv_local + [p for p in inv_pc if p["nom"].lower() not in noms_locaux]
        self._ds = self._load_display_settings()
        self._pc_info: dict = {}
        self._updating = False
        self._prod_mw_manual = False
        self._prod_mw = None
        self._prod_yield_manual = False
        self._prod_yield = 1.0
        self._prod_mass_manual = False
        self._prod_mass = 0.0
        self._prod_name = ""
        self._proc_content = ""
        self._chat_history = []
        self._chat_typing  = False
        self._rxn_solvant = ""
        self._rxn_temp    = ""
        self._rxn_time    = ""
        self._last_role   = None
        self._cells: dict[tuple, QLineEdit] = {}

        # Signaux
        self.sig_append.connect(self._append_proc_text)
        self.sig_done.connect(self._apply_proc_tags)
        self.sig_clear.connect(self._clear_proc_text)
        self.sig_chat_append.connect(self._append_chat_text)
        self.sig_chat_done.connect(self._chat_response_done)

        self._build_menubar()
        self._build_ui()
        self._build_form_fields()
        self._rebuild_table()
        self.statusBar().showMessage("Pret — Ajoutez un reactif Limitant pour commencer.")

    # =========================================================================
    # Barre de menus
    # =========================================================================
    def _build_menubar(self):
        mb = self.menuBar()

        def act(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            if tip:
                a.setStatusTip(tip)
            return a

        # Fichier
        fm = mb.addMenu("&Fichier")
        fm.addAction(act("Nouvelle reaction", self._reset, "Ctrl+N", "Reinitialiser la reaction"))
        fm.addSeparator()
        fm.addAction(act("Exporter CSV...", self._export_csv, "Ctrl+S"))
        fm.addAction(act("Exporter PDF...", self._export_pdf, "Ctrl+P"))
        fm.addSeparator()
        fm.addAction(act("Quitter", self.close, "Ctrl+Q"))

        # Edition
        em = mb.addMenu("&Edition")
        em.addAction(act("Tout effacer", self._reset))

        # Vue
        vm = mb.addMenu("&Vue")
        vm.addAction(act("Affichage...", self._open_display_settings, "Ctrl+D"))

        # Outils
        tm = mb.addMenu("&Outils")
        tm.addAction(act("Generer une procedure IA", self._generer_procedure, "Ctrl+G"))
        tm.addSeparator()
        tm.addAction(act("Cles API & fournisseur IA...", self._set_api_key_dialog))

        # Aide
        hm = mb.addMenu("&Aide")
        hm.addAction(act("A propos", self._about))

    # =========================================================================
    # Mise en page principale
    # =========================================================================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Bandeau header ──────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setFixedHeight(50)
        hdr.setStyleSheet(f"background: {C_HEAD}; border-bottom: 1px solid {BORDER};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 0, 16, 0)

        title = QLabel("Calculateur de Stochiometrie")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        hl.addWidget(title)
        hl.addStretch()

        hl.addWidget(self._dim_label("Reaction :"))
        self.rxn_name_edit = QLineEdit("Synthese")
        self.rxn_name_edit.setFixedSize(200, 28)
        self.rxn_name_edit.setStyleSheet(f"""
            QLineEdit {{
                border: 1px solid {ACCENT}; border-radius: 4px;
                padding: 2px 8px; font-size: 12px; color: {TEXT}; background: white;
            }}
        """)
        hl.addWidget(self.rxn_name_edit)
        root.addWidget(hdr)

        # ── Formulaire ──────────────────────────────────────────────────────
        self._form_group = QGroupBox("Ajouter un reactif")
        self._form_group.setStyleSheet(f"""
            QGroupBox {{
                font-weight: bold; font-size: 12px; color: {TEXT};
                border: 1px solid {BORDER}; border-radius: 8px;
                margin-top: 8px; padding-top: 6px;
                background: {PANEL};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 12px; padding: 0 4px;
                color: {TEXT};
            }}
        """)
        fg_outer = QVBoxLayout(self._form_group)
        fg_outer.setContentsMargins(12, 4, 12, 8)
        fg_outer.setSpacing(4)

        # Boutons Manuel / PubChem
        mode_bar = QHBoxLayout()
        mode_bar.addStretch()
        self.btn_manuel  = self._toggle_btn("Manuel",  checked=True)
        self.btn_pubchem = self._toggle_btn("PubChem", checked=False)
        self.btn_manuel.clicked.connect(lambda: self._on_mode("Manuel"))
        self.btn_pubchem.clicked.connect(lambda: self._on_mode("PubChem"))
        mode_bar.addWidget(self.btn_manuel)
        mode_bar.addWidget(self.btn_pubchem)
        fg_outer.addLayout(mode_bar)

        # Barre PubChem (cachee par defaut)
        self._pc_bar = QWidget()
        self._pc_bar.setStyleSheet("background: transparent;")
        pc_l = QHBoxLayout(self._pc_bar)
        pc_l.setContentsMargins(0, 0, 0, 0)
        self.pc_entry = QLineEdit()
        self.pc_entry.setPlaceholderText("Nom IUPAC ou CAS")
        self.pc_entry.setFixedSize(240, 30)
        self.pc_entry.setStyleSheet(
            f"QLineEdit {{ color: {TEXT}; background: white;"
            f" border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 6px; }}"
            f"QLineEdit:focus {{ border-color: {ACCENT}; }}"
        )
        self.pc_entry.returnPressed.connect(self._pc_search)
        pc_l.addWidget(self.pc_entry)
        self.pc_btn = QPushButton("Chercher")
        self.pc_btn.setFixedSize(100, 30)
        self.pc_btn.setStyleSheet(btn_style())
        self.pc_btn.clicked.connect(self._pc_search)
        pc_l.addWidget(self.pc_btn)
        self.pc_lbl = QLabel("")
        self.pc_lbl.setStyleSheet(f"color: {GREEN}; font-size: 11px; background: transparent;")
        pc_l.addWidget(self.pc_lbl)
        pc_l.addStretch()
        self._pc_bar.setVisible(False)
        fg_outer.addWidget(self._pc_bar)

        # Champs dynamiques
        self._form_fields_widget = QWidget()
        self._form_fields_widget.setStyleSheet("background: transparent;")
        self._form_fields_layout = QHBoxLayout(self._form_fields_widget)
        self._form_fields_layout.setContentsMargins(0, 0, 0, 0)
        self._form_fields_layout.setSpacing(8)
        fg_outer.addWidget(self._form_fields_widget)

        self.err_lbl = QLabel("")
        self.err_lbl.setStyleSheet(f"color: {RED}; font-size: 11px; background: transparent;")
        fg_outer.addWidget(self.err_lbl)

        form_wrapper = QWidget()
        form_wrapper.setStyleSheet(f"background: {BG};")
        fwl = QVBoxLayout(form_wrapper)
        fwl.setContentsMargins(8, 8, 8, 0)
        fwl.setSpacing(4)
        fwl.addWidget(self._form_group)

        # ── Splitter vertical : form | tableau | procedure | chat ─────────────
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setHandleWidth(8)
        self._splitter.setStyleSheet("""
            QSplitter::handle { background: #e2e8f0; border-radius: 2px; }
            QSplitter::handle:hover { background: #94a3b8; }
        """)

        # Pane 0 : formulaire (toujours visible, non collapsible)
        self._splitter.addWidget(form_wrapper)
        self._splitter.setCollapsible(0, False)

        # Pane 1 : tableau
        table_pane = QWidget()
        table_pane.setStyleSheet(f"background: {BG};")
        tl = QVBoxLayout(table_pane)
        tl.setContentsMargins(8, 4, 8, 4)
        tl.setSpacing(4)

        tbar = QHBoxLayout()
        tbl_title = QLabel("Tableau de reaction")
        tbl_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        tbl_title.setStyleSheet(f"color: {TEXT}; background: transparent;")
        tbar.addWidget(tbl_title)
        hint = self._dim_label("  Cellules blanches = saisie  |  Cellules grises = calcule")
        tbar.addWidget(hint)
        tbar.addStretch()
        self.info_lbl = QLabel("")
        self.info_lbl.setStyleSheet(f"color: {DIM}; font-size: 10px; background: transparent;")
        tbar.addWidget(self.info_lbl)
        tl.addLayout(tbar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"background: {BG};")
        tl.addWidget(self._scroll)
        self._splitter.addWidget(table_pane)
        self._splitter.setCollapsible(1, False)

        # Pane procedure
        self._proc_panel = QWidget()
        self._proc_panel.setStyleSheet(f"background: {PANEL};")
        pl = QVBoxLayout(self._proc_panel)
        pl.setContentsMargins(8, 4, 8, 8)
        pl.setSpacing(4)

        ph = QHBoxLayout()
        proc_title = QLabel("Procedure experimentale (IA)")
        proc_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        proc_title.setStyleSheet(f"color: {TEXT}; background: transparent;")
        ph.addWidget(proc_title)
        ph.addStretch()
        close_btn = QPushButton("x Fermer")
        close_btn.setFixedHeight(22)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {DIM}; border: none; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {RED}; }}"
        )
        close_btn.clicked.connect(self._hide_proc_panel)
        ph.addWidget(close_btn)
        pl.addLayout(ph)

        self._proc_txt = QTextEdit()
        self._proc_txt.setReadOnly(True)
        self._proc_txt.setFont(QFont("Segoe UI", 11))
        self._proc_txt.setStyleSheet(
            f"background: white; border: 1px solid {BORDER}; border-radius: 6px; color: {TEXT}; padding: 8px;"
        )
        pl.addWidget(self._proc_txt)
        self._splitter.addWidget(self._proc_panel)
        self._splitter.setCollapsible(2, True)
        self._proc_panel.setVisible(False)

        # ── Pane Chat (3e pane, cache par defaut) ───────────────────────────
        self._chat_panel = QWidget()
        self._chat_panel.setStyleSheet(f"background: {PANEL};")
        cl = QVBoxLayout(self._chat_panel)
        cl.setContentsMargins(8, 4, 8, 8)
        cl.setSpacing(4)

        ch = QHBoxLayout()
        chat_title = QLabel("Chat IA — Questions sur la procedure")
        chat_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        chat_title.setStyleSheet(f"color: {TEXT}; background: transparent;")
        ch.addWidget(chat_title)
        ch.addStretch()
        close_chat_btn = QPushButton("x Fermer")
        close_chat_btn.setFixedHeight(22)
        close_chat_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {DIM}; border: none; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {RED}; }}"
        )
        close_chat_btn.clicked.connect(lambda: self._chat_panel.setVisible(False))
        ch.addWidget(close_chat_btn)
        cl.addLayout(ch)

        self._chat_txt = QTextEdit()
        self._chat_txt.setReadOnly(True)
        self._chat_txt.setFont(QFont("Segoe UI", 10))
        self._chat_txt.setStyleSheet(
            f"background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; color: {TEXT}; padding: 6px;"
        )
        cl.addWidget(self._chat_txt)

        chat_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Votre question...")
        self._chat_input.setFixedHeight(32)
        self._chat_input.setStyleSheet(
            f"QLineEdit {{ color: {TEXT}; background: white; border: 1px solid {BORDER};"
            f" border-radius: 4px; padding: 2px 6px; }}"
            f"QLineEdit:focus {{ border-color: {ACCENT}; }}"
        )
        self._chat_input.returnPressed.connect(self._send_chat_message)
        chat_row.addWidget(self._chat_input)
        send_btn = QPushButton("Envoyer")
        send_btn.setFixedHeight(32)
        send_btn.setFixedWidth(80)
        send_btn.setStyleSheet(btn_style())
        send_btn.clicked.connect(self._send_chat_message)
        chat_row.addWidget(send_btn)
        cl.addLayout(chat_row)

        self._splitter.addWidget(self._chat_panel)
        self._splitter.setCollapsible(3, True)
        self._chat_panel.setVisible(False)

        # ── Footer ──────────────────────────────────────────────────────────
        foot = QWidget()
        foot.setStyleSheet(f"background: {PANEL}; border-top: 1px solid {BORDER};")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 6, 12, 6)
        fl.setSpacing(8)

        def fbtn(text, color, hover, slot):
            b = QPushButton(text)
            b.setFixedHeight(32)
            b.setStyleSheet(btn_style(color, hover))
            b.clicked.connect(slot)
            fl.addWidget(b)

        fbtn("Exporter CSV", "#16a34a", "#15803d", self._export_csv)
        fbtn("Exporter PDF", ACCENT,    "#1d4ed8", self._export_pdf)
        fbtn("Tout effacer", RED,       "#b91c1c", self._reset)
        fbtn("Procedure IA", "#7c3aed", "#6d28d9", self._generer_procedure)
        fbtn("Chat IA",      "#0e7490", "#0c6380", self._toggle_chat_panel)
        fl.addStretch()

        # Zone centrale scrollable (form + splitter)
        _main_scroll = QScrollArea()
        _main_scroll.setWidgetResizable(True)
        _main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        _main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        _main_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        _main_scroll.setStyleSheet(f'background: {BG};')
        _main_content = QWidget()
        _main_content.setStyleSheet(f'background: {BG};')
        _mc_layout = QVBoxLayout(_main_content)
        _mc_layout.setContentsMargins(0, 0, 0, 0)
        _mc_layout.setSpacing(0)
        _mc_layout.addWidget(self._splitter, 1)
        _main_content.setMinimumHeight(700)   # déclenche le scroll si fenêtre trop petite
        _main_scroll.setWidget(_main_content)
        root.addWidget(_main_scroll, 1)
        root.addWidget(foot)

    # =========================================================================
    # Widgets utilitaires
    # =========================================================================
    def _dim_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {DIM}; font-size: 10px; background: transparent;")
        return lbl

    def _toggle_btn(self, text, checked=False):
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setFixedSize(90, 28)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {PANEL}; color: {DIM}; border: 1px solid {BORDER};
                border-radius: 4px; font-size: 11px; font-weight: bold;
            }}
            QPushButton:checked {{
                background: {ACCENT}; color: white; border-color: {ACCENT};
            }}
            QPushButton:hover:!checked {{ background: {C_HEAD}; color: {ACCENT}; }}
        """)
        return btn

    def _form_field(self, label_text, width=110):
        """Retourne (container QWidget, QLineEdit)."""
        ds = self._ds
        col = QWidget()
        col.setStyleSheet("background: transparent;")
        vl = QVBoxLayout(col)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)
        lbl = QLabel(label_text)
        lbl_ff = ds.get("label_font_family", "Segoe UI")
        lbl_fs = ds.get("label_font_size", 9)
        lbl_c  = ds.get("label_color", DIM)
        _lf = QFont(lbl_ff, lbl_fs)
        _lf.setBold(ds.get("label_font_bold", True))
        _lf.setItalic(ds.get("label_font_italic", False))
        _lf.setUnderline(ds.get("label_font_underline", False))
        lbl.setFont(_lf)
        lbl.setStyleSheet(f"color: {lbl_c};")
        vl.addWidget(lbl)
        ent = QLineEdit()
        ent.setFixedSize(width, ds["field_h"])
        _ef = QFont(ds["font_family"], ds["font_size"])
        _ef.setBold(ds.get("font_bold", False))
        _ef.setItalic(ds.get("font_italic", False))
        _ef.setUnderline(ds.get("font_underline", False))
        ent.setFont(_ef)
        tc = ds["text_color"]
        ent.setStyleSheet(f"""
            QLineEdit {{
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 2px 6px; background: white; color: {tc};
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """)
        vl.addWidget(ent)
        return col, ent

    # =========================================================================
    # Formulaire dynamique
    # =========================================================================
    def _build_form_fields(self):
        # Vider les anciens champs (widgets ET spacers)
        while self._form_fields_layout.count():
            item = self._form_fields_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        has_lim = any(r["role"] == "Limitant" for r in self.reagents)
        role_opts = (
            ["Reactif", "Solvant", "Catalyseur", "Autre"]
            if has_lim else
            ["Limitant", "Reactif", "Solvant", "Catalyseur", "Autre"]
        )
        if self._last_role not in role_opts:
            self._last_role = role_opts[0]
        is_lim = (self._last_role == "Limitant")

        ds = self._ds

        # Nom
        col_nom, self.f_name = self._form_field("Nom *", ds["field_w_name"])
        self._form_fields_layout.addWidget(col_nom)
        self._setup_autocomplete(self.f_name)

        # MW
        col_mw, self.f_mw = self._form_field("MW (g/mol) *", ds["field_w_mw"])
        self._form_fields_layout.addWidget(col_mw)

        # Masse ou Eq
        if is_lim:
            col_m, self.f_mass = self._form_field("Masse (g) *", ds["field_w_mass"])
            self._form_fields_layout.addWidget(col_m)
            self.f_eq = None
        else:
            col_e, self.f_eq = self._form_field("Eq *", max(60, ds["field_w_mass"] - 30))
            self.f_eq.setText("1")
            self._form_fields_layout.addWidget(col_e)
            self.f_mass = None

        # Role
        col_r = QWidget(); col_r.setStyleSheet("background: transparent;")
        vl_r = QVBoxLayout(col_r); vl_r.setContentsMargins(0, 0, 0, 0); vl_r.setSpacing(2)
        rl = QLabel("Role")
        _rl_f = QFont(ds.get("label_font_family", "Segoe UI"), ds.get("label_font_size", 9))
        _rl_f.setBold(ds.get("label_font_bold", True))
        _rl_f.setItalic(ds.get("label_font_italic", False))
        _rl_f.setUnderline(ds.get("label_font_underline", False))
        rl.setFont(_rl_f)
        rl.setStyleSheet(f"color: {ds.get('label_color', DIM)};")
        vl_r.addWidget(rl)
        self.f_role = QComboBox()
        self.f_role.addItems(role_opts)
        self.f_role.setCurrentText(self._last_role)
        self.f_role.setFixedSize(120, ds["field_h"])
        _rf = QFont(ds["font_family"], ds["font_size"])
        _rf.setBold(ds.get("font_bold", False))
        _rf.setItalic(ds.get("font_italic", False))
        _rf.setUnderline(ds.get("font_underline", False))
        self.f_role.setFont(_rf)
        self.f_role.setStyleSheet(f"""
            QComboBox {{
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 2px 6px; background: white; color: {TEXT};
            }}
            QComboBox:focus {{ border-color: {ACCENT}; }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox QAbstractItemView {{
                background: white; border: 1px solid {BORDER};
                selection-background-color: {C_HEAD};
                selection-color: {ACCENT};
            }}
        """)
        self.f_role.currentTextChanged.connect(self._on_role)
        vl_r.addWidget(self.f_role)
        self._form_fields_layout.addWidget(col_r)

        # Purete
        col_p, self.f_pur = self._form_field("Purete (%)", ds["field_w_pur"])
        self.f_pur.setText("100")
        self._form_fields_layout.addWidget(col_p)

        # Densite (champ cache)
        self.f_dens = QLineEdit()
        self.f_dens.setVisible(False)

        # Bouton Ajouter
        col_btn = QWidget(); col_btn.setStyleSheet("background: transparent;")
        vbl = QVBoxLayout(col_btn); vbl.setContentsMargins(0, 0, 0, 0); vbl.setSpacing(2)
        vbl.addWidget(QLabel(""))  # alignement vertical
        add_btn = QPushButton("Ajouter")
        add_btn.setFixedSize(110, ds["field_h"])
        add_btn.setStyleSheet(btn_style())
        add_btn.clicked.connect(self._add)
        vbl.addWidget(add_btn)
        self._form_fields_layout.addWidget(col_btn)
        self._form_fields_layout.addStretch()

        # Pre-remplissage PubChem
        if self._pc_info:
            self.f_name.setText(self._pc_info.get("name", ""))
            self.f_mw.setText(str(self._pc_info.get("mw") or ""))

    def _setup_autocomplete(self, entry: QLineEdit):
        # Préparer la liste d'affichage (nom uniquement)
        self._inv_display = [p["nom"] for p in self._inventaire]
        # Créer le completer avec une liste vide au départ
        comp = _ContainsCompleter([], entry)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setMaxVisibleItems(12)
        comp.activated[str].connect(self._on_inv_selected)
        entry.setCompleter(comp)
        # Filtrage dynamique sur chaque frappe
        entry.textEdited.connect(lambda text: self._filter_inv_completer(entry, text))

    def _filter_inv_completer(self, entry: QLineEdit, text: str):
        if len(text) < 2:
            entry.completer().setModel(QStringListModel([]))
            return
        matches = [s for s in self._inv_display if text.lower() in s.lower()][:15]
        entry.completer().setModel(QStringListModel(matches))
        entry.completer().complete()

    def _filter_solv_completer(self, entry: QLineEdit, text: str):
        if not text:
            entry.completer().setModel(QStringListModel([]))
            return
        matches = [s for s in SOLVANTS_USUELS if text.lower() in s.lower()]
        entry.completer().setModel(QStringListModel(matches))
        entry.completer().complete()

    def _on_inv_selected(self, text: str):
        self.f_name.setText(text)
        for p in self._inventaire:
            if p["nom"] == text and p["mw"] is not None:
                self.f_mw.setText(str(p["mw"]))
                break

    # =========================================================================
    # Logique formulaire
    # =========================================================================
    def _on_mode(self, mode):
        self.btn_manuel.setChecked(mode == "Manuel")
        self.btn_pubchem.setChecked(mode == "PubChem")
        self._pc_bar.setVisible(mode == "PubChem")

    def _on_role(self, val):
        self._last_role = val
        name_val = self.f_name.text()
        mw_val   = self.f_mw.text()
        # Differer le rebuild : Qt traite encore l'evenement de selection du QComboBox,
        # detruire le widget pendant ce temps provoque un crash.
        QTimer.singleShot(0, lambda nv=name_val, mv=mw_val: self._rebuild_form_after_role(nv, mv))

    def _rebuild_form_after_role(self, name_val, mw_val):
        self._build_form_fields()
        if name_val: self.f_name.setText(name_val)
        if mw_val:   self.f_mw.setText(mw_val)

    def _pc_search(self):
        q = self.pc_entry.text().strip()
        if not q:
            return
        self.pc_btn.setText("...")
        self.pc_btn.setEnabled(False)
        self.pc_lbl.setText("Recherche...")
        self.pc_lbl.setStyleSheet(f"color: {DIM}; font-size: 11px; background: transparent;")
        threading.Thread(target=self._do_search, args=(q,), daemon=True).start()

    def _do_search(self, q):
        try:
            cpds = pcp.get_compounds(q, "name") or pcp.get_compounds(q, "formula")
            res = (
                {"name": cpds[0].iupac_name or q,
                 "mw":   float(cpds[0].molecular_weight) if cpds[0].molecular_weight else None,
                 "formula": cpds[0].molecular_formula or ""}
                if cpds else {"error": "Introuvable sur PubChem"}
            )
        except Exception as e:
            res = {"error": str(e)}
        QTimer.singleShot(0, lambda: self._search_done(res))

    def _search_done(self, res):
        self.pc_btn.setText("Chercher")
        self.pc_btn.setEnabled(True)
        if "error" in res:
            msg = res["error"]
            self.pc_lbl.setText(msg)
            self.pc_lbl.setStyleSheet(f"color: {RED}; font-size: 11px; background: transparent;")
            # Si erreur réseau ou module manquant, afficher une boîte de dialogue
            if any(k in msg.lower() for k in ("connection", "timeout", "module", "ssl", "urlopen")):
                QMessageBox.warning(self, "Erreur PubChem",
                    f"Impossible de contacter PubChem :\n{msg}\n\n"
                    "Vérifiez votre connexion internet.")
        else:
            self.pc_lbl.setText(f"OK  {res['name']}  MW={res['mw']}  {res['formula']}")
            self.pc_lbl.setStyleSheet(f"color: {GREEN}; font-size: 11px; background: transparent;")
            self.f_name.setText(res["name"])
            self.f_mw.setText(str(res["mw"] or ""))

    def _add(self):
        self.err_lbl.setText("")
        name = self.f_name.text().strip()
        if not name:
            self.err_lbl.setText("Nom obligatoire."); return
        try:
            mw = float(self.f_mw.text().replace(",", ".")); assert mw > 0
        except (ValueError, AssertionError):
            self.err_lbl.setText("MW invalide."); return
        try:
            purity = float(self.f_pur.text().replace(",", ".")); assert 0 < purity <= 100
        except (ValueError, AssertionError):
            self.err_lbl.setText("Purete invalide (0-100)."); return

        role = self.f_role.currentText()
        if role == "Limitant":
            if any(r["role"] == "Limitant" for r in self.reagents):
                self.err_lbl.setText("Un Limitant existe deja."); return
            try:
                mass_g = float(self.f_mass.text().replace(",", ".")); assert mass_g > 0
            except (ValueError, AssertionError):
                self.err_lbl.setText("Masse obligatoire pour le Limitant."); return
            eq = 1.0
        else:
            try:
                eq = float(self.f_eq.text().replace(",", ".")); assert eq > 0
            except (ValueError, AssertionError):
                self.err_lbl.setText("Equivalents invalides."); return
            mass_g = 0.0

        dens_s = self.f_dens.text().strip().replace(",", ".")
        density = float(dens_s) if dens_s else 0.0

        self.reagents.append({
            "name": name, "mw": mw, "purity": purity,
            "role": role, "eq": eq, "mass_g": mass_g, "density": density,
        })
        self._prod_mw_manual = False
        self._prod_mw = None
        self._rebuild_table()
        self._last_role = "Reactif" if role == "Limitant" else role
        self._pc_info = {}
        self.pc_lbl.setText("")
        self._build_form_fields()

    def _delete_col(self, idx):
        self.reagents.pop(idx)
        self._prod_mw_manual = False
        self._prod_mw = None
        self._rebuild_table()
        self._build_form_fields()

    def _change_role(self, idx, new_role):
        r = self.reagents[idx]
        if r["role"] == new_role:
            return
        if new_role == "Limitant":
            # Récupère la masse calculée si disponible, sinon mol×MW
            mass = self._get_cell(idx, "mass_g")
            if not mass:
                mol = self._get_cell(idx, "mol")
                mw  = self._get_cell(idx, "mw")
                pur = r.get("purity", 100) or 100
                mass = mol * mw / (pur / 100.0) if (mol and mw) else 0.0
            r["mass_g"] = mass
            r["eq"] = 1.0
        elif r["role"] == "Limitant":
            # Était limitant → devient autre : passe en mode eq
            r["mass_g"] = 0.0
            r["eq"] = 1.0
        r["role"] = new_role
        # Defer pour éviter le crash Qt (combobox encore dans son event handler)
        QTimer.singleShot(0, self._rebuild_and_form)

    def _rebuild_and_form(self):
        self._rebuild_table()
        self._build_form_fields()

    def _reset(self):
        if self.reagents:
            r = QMessageBox.question(
                self, "Reinitialiser", "Effacer toute la reaction ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self.reagents.clear()
        self._cells.clear()
        self._prod_mw_manual = False
        self._prod_mw = None
        self._prod_yield_manual = False
        self._prod_yield = 1.0
        self._prod_mass_manual = False
        self._prod_mass = 0.0
        self._prod_name = ""
        self._last_role = None
        self._rxn_solvant = ""
        self._rxn_temp = ""
        self._rxn_time = ""
        self._rebuild_table()
        self.info_lbl.setText("")
        self._build_form_fields()

    # =========================================================================
    # Tableau
    # =========================================================================
    def _draw_empty(self):
        w = QLabel("Ajoutez un reactif Limitant pour commencer.")
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w.setStyleSheet(f"color: {DIM}; font-size: 14px; padding: 50px; background: {BG};")
        self._scroll.setWidget(w)

    def _make_data_cell(self, bg, fg, bold=False, readonly=False, border=None,
                        col_w=None, row_h=None, font_family=None, font_size=None,
                        style_bold=False, style_italic=False, style_underline=False):
        ds = self._ds
        e = QLineEdit()
        e.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(font_family or ds.get("tbl_cell_font_family", ds["font_family"]),
                  font_size  or ds.get("tbl_cell_font_size",   ds["font_size"] + 2))
        f.setBold(bold or style_bold)
        f.setItalic(style_italic)
        f.setUnderline(style_underline)
        e.setFont(f)
        e.setFixedSize(col_w or ds["tbl_col_w"], row_h or ds["tbl_row_h"])
        bc = border or (BORDER if readonly else BORDER_IN)
        e.setStyleSheet(cell_style(bg, fg, bc))
        if readonly:
            e.setReadOnly(True)
        return e

    # =========================================================================
    def _rebuild_table(self):
        self._cells.clear()
        n = len(self.reagents)
        prod_col = n
        # Dimensions lues depuis les paramètres d'affichage
        ds     = self._ds
        COL_W  = ds["tbl_col_w"]
        ROW_H  = ds["tbl_row_h"]
        HDR_H  = ds["tbl_hdr_h"]
        PROP_W = ds["tbl_prop_w"]

        container = QWidget()
        container.setStyleSheet(f"background: {BG};")
        grid = QGridLayout(container)
        grid.setSpacing(1)
        grid.setContentsMargins(2, 2, 2, 2)

        # ── Ligne 0 : en-tetes ────────────────────────────────────────────
        # Coin vide
        corner = QWidget()
        corner.setFixedWidth(PROP_W)
        corner.setMinimumHeight(HDR_H)
        corner.setStyleSheet(f"background: {C_LABEL}; border: 1px solid {BORDER};")
        grid.addWidget(corner, 0, 0)

        # En-tetes reactifs
        for i, r in enumerate(self.reagents):
            is_lim = r["role"] == "Limitant"
            hdr_bg_base = ds.get("tbl_hdr_bg", C_HEAD)
            # Teinte légèrement plus claire pour le limitant
            bg = C_HEAD_L if (is_lim and hdr_bg_base == C_HEAD) else hdr_bg_base
            frame = QWidget()
            frame.setFixedWidth(COL_W)
            frame.setMinimumHeight(HDR_H)
            frame.setStyleSheet(f"background: {bg}; border: 1px solid {BORDER};")
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(4, 4, 4, 4)
            fl.setSpacing(2)

            top_row = QHBoxLayout()
            top_row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(_wrap_name(r["name"]))
            _hdr_f = QFont(ds.get("tbl_hdr_font_family", "Segoe UI"), ds.get("tbl_hdr_font_size", 8))
            _hdr_f.setBold(ds.get("tbl_hdr_bold", True))
            _hdr_f.setItalic(ds.get("tbl_hdr_italic", False))
            _hdr_f.setUnderline(ds.get("tbl_hdr_underline", False))
            name_lbl.setFont(_hdr_f)
            hdr_tc = ds.get("tbl_hdr_color", ACCENT)
            name_lbl.setStyleSheet(f"color: {hdr_tc}; background: transparent; border: none;")
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setWordWrap(True)
            top_row.addWidget(name_lbl, 1)

            del_btn = QPushButton("x")
            del_btn.setFixedSize(18, 18)
            del_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {RED};
                    border: none; font-weight: bold; font-size: 11px;
                    border-radius: 3px;
                }}
                QPushButton:hover {{ background: #fee2e2; }}
            """)
            del_btn.clicked.connect(lambda _, idx=i: self._delete_col(idx))
            top_row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignTop)
            fl.addLayout(top_row)

            # Menu déroulant pour changer le rôle
            has_other_lim = any(
                r2["role"] == "Limitant"
                for j, r2 in enumerate(self.reagents) if j != i
            )
            role_opts = ["Limitant", "Reactif", "Solvant", "Catalyseur", "Autre"]
            if has_other_lim:
                role_opts.remove("Limitant")
            role_combo = QComboBox()
            role_combo.addItems(role_opts)
            role_combo.setCurrentText(r["role"])
            role_combo.setFixedHeight(22)
            role_combo.setStyleSheet(f"""
                QComboBox {{
                    border: 1px solid {BORDER}; border-radius: 3px;
                    padding: 1px 4px; background: transparent; color: {DIM};
                    font-size: 9px;
                }}
                QComboBox:focus {{ border-color: {ACCENT}; }}
                QComboBox::drop-down {{ border: none; width: 16px; }}
                QComboBox QAbstractItemView {{
                    background: white; border: 1px solid {BORDER};
                    selection-background-color: {C_HEAD};
                    selection-color: {ACCENT};
                }}
            """)
            role_combo.currentTextChanged.connect(
                lambda val, idx=i: self._change_role(idx, val)
            )
            fl.addWidget(role_combo)
            grid.addWidget(frame, 0, i + 1)

        # En-tete fleche
        arrow_col     = n + 1
        prod_col_grid = n + 2

        arr_frame = QWidget()
        arr_frame.setFixedWidth(COL_W)
        arr_frame.setMinimumHeight(HDR_H)
        arr_frame.setStyleSheet(f"background: {BG}; border: 1px solid {BORDER};")
        avl = QVBoxLayout(arr_frame)
        avl.setContentsMargins(4, 4, 4, 4)
        avl.setSpacing(2)
        catalysts = [r["name"] for r in self.reagents if r["role"] == "Catalyseur"]
        if catalysts:
            cat_lbl = QLabel(" / ".join(catalysts))
            cat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cat_lbl.setFont(QFont("Segoe UI", 8))
            cat_lbl.setStyleSheet(
                f"color: {DIM}; background: transparent; border: none; font-style: italic;"
            )
            avl.addWidget(cat_lbl)
        arrow_lbl = QLabel("→")
        arrow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow_lbl.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
        arrow_color = DIM if n == 0 else TEXT
        arrow_lbl.setStyleSheet(f"color: {arrow_color}; background: transparent; border: none;")
        avl.addWidget(arrow_lbl, 1)
        grid.addWidget(arr_frame, 0, arrow_col)

        # En-tete produit
        prod_hdr = QWidget()
        prod_hdr.setFixedWidth(COL_W)
        prod_hdr.setMinimumHeight(HDR_H)
        prod_hdr.setStyleSheet(f"background: {ds.get('tbl_prod_bg', C_PROD)}; border: 1px solid {BORDER};")
        pvl = QVBoxLayout(prod_hdr)
        pvl.setContentsMargins(4, 4, 4, 4)

        prod_title = self._prod_name if self._prod_name else "Produit"
        pl1 = QLabel(_wrap_name(prod_title))
        pl1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _pl1_f = QFont(ds.get("tbl_hdr_font_family", "Segoe UI"), 8 if self._prod_name else 10)
        _pl1_f.setBold(ds.get("tbl_hdr_bold", True))
        _pl1_f.setItalic(ds.get("tbl_hdr_italic", False))
        _pl1_f.setUnderline(ds.get("tbl_hdr_underline", False))
        pl1.setFont(_pl1_f)
        pl1.setStyleSheet(f"color: {ds.get('tbl_prod_color', GREEN)}; background: transparent; border: none;")
        pl1.setWordWrap(True)
        if self._prod_name:
            pl1.setToolTip(self._prod_name)
        yield_txt = f"rendement {int(self._prod_yield * 100)}%" if self._prod_yield_manual else ""
        pl2 = QLabel(yield_txt)
        pl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pl2.setFont(QFont("Segoe UI", 9))
        pl2.setStyleSheet(f"color: {DIM}; background: transparent; border: none;")
        pvl.addWidget(pl1)
        pvl.addWidget(pl2)
        grid.addWidget(prod_hdr, 0, prod_col_grid)

        # ── Lignes de donnees ─────────────────────────────────────────────
        rxn_conditions = [
            ("Solvant", "_rxn_solvant"),
            ("T (°C)",  "_rxn_temp"),
            ("t (h)",   "_rxn_time"),
        ]

        # Repartir les lignes du tableau equitablement entre les 3 cellules conditions
        _n_data = len(ROWS)
        _n_cond = len(rxn_conditions)
        _base   = _n_data // _n_cond
        _extra  = _n_data % _n_cond
        _cond_spans = [_base + (1 if i < _extra else 0) for i in range(_n_cond)]
        # cond_row_map : r_grid de depart -> (label, attr, span)
        _cond_row_map: dict[int, tuple] = {}
        _cur = 1
        for _ci, (_cl, _ca) in enumerate(rxn_conditions):
            _cond_row_map[_cur] = (_cl, _ca, _cond_spans[_ci])
            _cur += _cond_spans[_ci]
        # ensemble des lignes couvertes par un span (pas de cellule vide a ajouter)
        _covered: set[int] = set()
        for _s, (_, _, _sp) in _cond_row_map.items():
            _covered.update(range(_s, _s + _sp))

        for row_idx, (label, key, ed_lim, ed_react, ed_prod) in enumerate(ROWS):
            r_grid = row_idx + 1

            # Etiquette
            lbl_w = QWidget()
            lbl_w.setFixedSize(PROP_W, ROW_H)
            lbl_bg = ds.get("tbl_lbl_bg", C_LABEL)
            lbl_w.setStyleSheet(f"background: {lbl_bg}; border: 1px solid {BORDER};")
            ll = QLabel(label, lbl_w)
            _lbl_f = QFont(ds.get("tbl_lbl_font_family", "Segoe UI"), ds.get("tbl_lbl_font_size", 10))
            _lbl_f.setBold(ds.get("tbl_lbl_bold", True))
            _lbl_f.setItalic(ds.get("tbl_lbl_italic", False))
            _lbl_f.setUnderline(ds.get("tbl_lbl_underline", False))
            ll.setFont(_lbl_f)
            lbl_tc = ds.get("tbl_lbl_color", ACCENT)
            ll.setStyleSheet(
                f"color: {lbl_tc}; background: transparent; border: none; padding: 0 8px;"
            )
            ll.setFixedSize(PROP_W, ROW_H)
            ll.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(lbl_w, r_grid, 0)

            is_mass = (key == "mass_g")

            # Cellules reactifs
            for i, r in enumerate(self.reagents):
                is_lim   = r["role"] == "Limitant"
                editable = ed_lim if is_lim else ed_react
                bg       = ds.get("tbl_cell_bg_input", C_INPUT) if editable else ds.get("tbl_cell_bg_calc", C_CALC)
                fg       = RED if is_mass else ds.get("tbl_cell_color", TEXT)
                cell = self._make_data_cell(bg, fg, bold=is_mass, readonly=not editable,
                                            col_w=COL_W, row_h=ROW_H,
                                            style_bold=ds.get("tbl_cell_bold", False),
                                            style_italic=ds.get("tbl_cell_italic", False),
                                            style_underline=ds.get("tbl_cell_underline", False))
                self._cells[(i, key)] = cell
                grid.addWidget(cell, r_grid, i + 1)
                if editable:
                    cell.textChanged.connect(
                        lambda _, col=i, k=key: self._on_input(col, k)
                    )

            # Colonne fleche : cellule condition avec rowspan (s'etend sur plusieurs lignes)
            if r_grid in _cond_row_map:
                cond_lbl_txt, cond_attr, cond_span = _cond_row_map[r_grid]
                # Hauteur = N lignes * ROW_H + (N-1) espaces de 1px
                cell_h = cond_span * ROW_H + (cond_span - 1) * 1
                cond_w = QWidget()
                cond_w.setFixedSize(COL_W, cell_h)
                cond_bg = ds.get("tbl_cond_bg", C_COND)
                cond_w.setStyleSheet(f"background: {cond_bg}; border: 1px solid {BORDER};")
                cvl = QVBoxLayout(cond_w)
                cvl.setContentsMargins(4, 6, 4, 6)
                cvl.setSpacing(4)

                clbl = QLabel(cond_lbl_txt)
                clbl.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                clbl.setStyleSheet(f"color: {DIM}; background: transparent; border: none;")
                clbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cvl.addWidget(clbl)

                cond_fg  = ds.get("tbl_cond_color", TEXT)
                cond_ff  = ds.get("tbl_cond_font_family", "Segoe UI")
                cond_fs  = ds.get("tbl_cond_font_size", 13)
                cond_bold = ds.get("tbl_cond_bold", False)
                cond_ital = ds.get("tbl_cond_italic", False)
                cond_und  = ds.get("tbl_cond_underline", False)
                avail_w   = COL_W - 24  # marges cond_w (8) + padding QLineEdit (8) + marge (8)

                def _make_cond_font(text, family=cond_ff, max_sz=cond_fs,
                                    bold=cond_bold, ital=cond_ital, und=cond_und, w=avail_w):
                    sz = max_sz
                    while sz > 6:
                        if QFontMetrics(QFont(family, sz)).horizontalAdvance(text) <= w:
                            break
                        sz -= 1
                    f = QFont(family, sz)
                    f.setBold(bold); f.setItalic(ital); f.setUnderline(und)
                    return f

                cent = QLineEdit(getattr(self, cond_attr))
                cent.setFont(_make_cond_font(cent.text()))
                cent.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cent.setStyleSheet(f"""
                    QLineEdit {{
                        background: white; color: {cond_fg};
                        border: 1px solid {BORDER}; border-radius: 4px; padding: 4px;
                    }}
                    QLineEdit:focus {{ border-color: {ACCENT}; border-width: 2px; }}
                """)

                def _on_cond_change(text, attr=cond_attr, le=cent):
                    setattr(self, attr, text)
                    le.setFont(_make_cond_font(text))

                cent.textChanged.connect(_on_cond_change)
                cvl.addWidget(cent)
                cvl.addStretch()

                # Autocompletion solvant (meme logique "contains" que l'inventaire)
                if cond_lbl_txt == "Solvant":
                    solv_comp = _ContainsCompleter([], cent)
                    solv_comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                    solv_comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                    solv_comp.setMaxVisibleItems(10)
                    cent.setCompleter(solv_comp)
                    cent.textEdited.connect(lambda text, c=cent: self._filter_solv_completer(c, text))

                grid.addWidget(cond_w, r_grid, arrow_col, cond_span, 1)
            # si la ligne est couverte par un span precedent, on n'ajoute rien

            # Cellule produit
            bg_p   = ds.get("tbl_prod_bg", C_PROD) if ed_prod else ds.get("tbl_prod_bg_calc", C_PROD_C)
            fg_p   = RED if is_mass else (ds.get("tbl_prod_color", GREEN) if ed_prod else DIM)
            cell_p = self._make_data_cell(bg_p, fg_p, bold=is_mass, readonly=not ed_prod,
                                          col_w=COL_W, row_h=ROW_H,
                                          font_family=ds.get("tbl_prod_font_family"),
                                          font_size=ds.get("tbl_prod_font_size"),
                                          style_bold=ds.get("tbl_prod_bold", False),
                                          style_italic=ds.get("tbl_prod_italic", False),
                                          style_underline=ds.get("tbl_prod_underline", False))
            self._cells[(prod_col, key)] = cell_p
            grid.addWidget(cell_p, r_grid, prod_col_grid)
            if ed_prod:
                cell_p.textChanged.connect(lambda _, k=key: self._on_prod_input(k))

        # Largeurs de colonnes minimales
        grid.setColumnMinimumWidth(0, PROP_W)
        for c in range(1, n + 1):
            grid.setColumnMinimumWidth(c, COL_W)
        grid.setColumnMinimumWidth(arrow_col, COL_W)
        grid.setColumnMinimumWidth(prod_col_grid, COL_W)

        # Valeurs initiales
        for i, r in enumerate(self.reagents):
            self._set_cell(i, "mw",      fmt(r["mw"], 4))
            self._set_cell(i, "purity",  fmt(r["purity"], 1))
            self._set_cell(i, "density", fmt(r["density"], 4) if r["density"] else "")
            if r["role"] == "Limitant":
                self._set_cell(i, "mass_g", fmt(r["mass_g"], 5))
                self._set_cell(i, "eq", "1")
            else:
                self._set_cell(i, "eq", fmt(r["eq"], 3))

        self._set_cell(prod_col, "purity", "100")
        if self._prod_yield_manual and not self._prod_mass_manual:
            self._set_cell(prod_col, "eq", fmt(self._prod_yield, 3))
        if self._prod_mass_manual:
            self._set_cell(prod_col, "mass_g", fmt(self._prod_mass, 5))
        if self._prod_mw is not None:
            self._set_cell(prod_col, "mw", fmt(self._prod_mw, 3))

        self._scroll.setWidget(container)
        self._recalc()

    # =========================================================================
    # Recalcul stochiometrique
    # =========================================================================
    def _set_cell(self, col, key, text):
        w = self._cells.get((col, key))
        if w:
            w.blockSignals(True)
            w.setText(text)
            w.blockSignals(False)

    def _get_cell(self, col, key):
        w = self._cells.get((col, key))
        return _f(w.text()) if w else 0.0

    def _on_input(self, col, key):
        if self._updating: return
        try:
            v = float(self._cells[(col, key)].text().replace(",", "."))
        except ValueError:
            return
        self.reagents[col][key] = v
        self._recalc()

    def _on_prod_input(self, key):
        if self._updating: return
        prod_col = len(self.reagents)
        if key == "mw":
            self._prod_mw_manual = True
            try:
                self._prod_mw = float(self._cells[(prod_col, "mw")].text().replace(",", "."))
            except ValueError:
                pass
        elif key == "eq":
            # Mode rendement → désactive le mode masse manuelle
            self._prod_mass_manual = False
            try:
                v = float(self._cells[(prod_col, "eq")].text().replace(",", "."))
                self._prod_yield = v
                self._prod_yield_manual = True
            except ValueError:
                pass
        elif key == "mass_g":
            # Mode masse → désactive le mode rendement manuel
            self._prod_yield_manual = False
            try:
                v = float(self._cells[(prod_col, "mass_g")].text().replace(",", "."))
                self._prod_mass = v
                self._prod_mass_manual = True
            except ValueError:
                self._prod_mass_manual = False
        self._recalc()

    def _recalc(self):
        if self._updating: return
        self._updating = True
        n = len(self.reagents)
        if n == 0:
            self._updating = False; return

        prod_col = n

        # Lire les entrees
        for i, r in enumerate(self.reagents):
            r["mw"]      = self._get_cell(i, "mw")
            r["purity"]  = self._get_cell(i, "purity") or 100.0
            r["density"] = self._get_cell(i, "density")
            if r["role"] == "Limitant":
                r["mass_g"] = self._get_cell(i, "mass_g")
            else:
                r["eq"] = self._get_cell(i, "eq") or 1.0

        # Moles du limitant
        lim = next((r for r in self.reagents if r["role"] == "Limitant"), None)
        n_lim = None
        if lim and lim["mw"] > 0 and lim["mass_g"] > 0:
            n_lim = lim["mass_g"] * (lim["purity"] / 100.0) / lim["mw"]

        # Reactifs
        for i, r in enumerate(self.reagents):
            is_lim  = r["role"] == "Limitant"
            mw      = r["mw"]
            purity  = r["purity"]
            density = r["density"]
            if is_lim:
                mol    = n_lim or 0.0
                mass_g = r["mass_g"]
                eq_v   = 1.0
            else:
                eq_v   = r["eq"]
                mol    = eq_v * n_lim if n_lim else 0.0
                mass_g = mol * mw / (purity / 100.0) if (mw and purity > 0) else 0.0
            volume = (mass_g / density) if (density and mass_g) else 0.0

            self._set_cell(i, "mol",    f"{mol:.6f}"    if mol    else "")
            self._set_cell(i, "volume", f"{volume:.5f}" if volume else "")
            if not is_lim:
                self._set_cell(i, "mass_g", f"{mass_g:.5f}" if mass_g else "")
                self._set_cell(i, "eq",     f"{eq_v:.3f}")
            else:
                self._set_cell(i, "eq", "1")

        # Produit
        prod_mw   = self._get_cell(prod_col, "mw")
        prod_dens = self._get_cell(prod_col, "density")
        prod_mol  = n_lim or 0.0

        if self._prod_mass_manual:
            # Mode masse → back-calcule le rendement
            prod_mass = self._prod_mass
            if prod_mol and prod_mw:
                prod_yield = prod_mass / (prod_mol * prod_mw)
                self._prod_yield = prod_yield
                self._set_cell(prod_col, "eq", f"{prod_yield:.3f}")
            prod_vol = (prod_mass / prod_dens) if (prod_dens and prod_mass) else 0.0
            self._set_cell(prod_col, "mol",    f"{prod_mol:.6f}"  if prod_mol  else "")
            # Ne pas écraser mass_g — c'est la valeur saisie par l'utilisateur
            self._set_cell(prod_col, "volume", f"{prod_vol:.5f}"  if prod_vol  else "")
        else:
            # Mode rendement → calcule la masse
            prod_yield = self._prod_yield
            prod_mass = prod_mol * prod_mw * prod_yield if prod_mw else 0.0
            prod_vol  = (prod_mass / prod_dens) if (prod_dens and prod_mass) else 0.0
            self._set_cell(prod_col, "mol",    f"{prod_mol:.6f}"  if prod_mol  else "")
            self._set_cell(prod_col, "mass_g", f"{prod_mass:.5f}" if prod_mass else "")
            self._set_cell(prod_col, "volume", f"{prod_vol:.5f}"  if prod_vol  else "")

        self._set_cell(prod_col, "purity", "100")

        # Barre d'info
        if n_lim:
            self.info_lbl.setText(
                f"n(limitant) = {n_lim:.6f} mol  •  Produit theorique = {prod_mass*1000:.3f} mg"
            )
        else:
            self.info_lbl.setText("Definissez un Limitant avec sa masse.")

        self._updating = False

    # =========================================================================
    # Panneau procedure
    # =========================================================================
    def _show_proc_panel(self):
        self._proc_panel.show()
        sizes = list(self._splitter.sizes())
        # pane 2 = proc (index 2 car form=0, table=1, proc=2, chat=3)
        if len(sizes) >= 3 and sizes[2] == 0:
            give = 280
            sizes[1] = max(80, sizes[1] - give)
            sizes[2] = give
            self._splitter.setSizes(sizes)

    def _hide_proc_panel(self):
        sizes = list(self._splitter.sizes())
        if len(sizes) >= 3:
            sizes[1] += sizes[2]
            sizes[2] = 0
            self._splitter.setSizes(sizes)
        self._proc_panel.hide()

    def _toggle_chat_panel(self):
        sizes = list(self._splitter.sizes())
        chat_visible = self._chat_panel.isVisible() and len(sizes) >= 4 and sizes[3] > 0
        if chat_visible:
            # Fermer le chat
            if len(sizes) >= 4:
                sizes[1] += sizes[3]
                sizes[3] = 0
                self._splitter.setSizes(sizes)
            self._chat_panel.hide()
        else:
            # Ouvrir le chat
            self._chat_panel.show()
            sizes = list(self._splitter.sizes())
            if len(sizes) >= 4 and sizes[3] == 0:
                give = 220
                sizes[1] = max(80, sizes[1] - give)
                sizes[3] = give
                self._splitter.setSizes(sizes)
            self._chat_input.setFocus()

    def _clear_proc_text(self):
        self._proc_txt.clear()
        self._proc_content = ""
        self._chat_txt.clear()
        self._chat_history = []
        self._chat_typing = False
        self._chat_input.setEnabled(True)

    def _append_proc_text(self, text):
        self._proc_content += text
        cursor = self._proc_txt.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._proc_txt.setTextCursor(cursor)
        self._proc_txt.ensureCursorVisible()

    def _apply_proc_tags(self):
        doc = self._proc_txt.document()
        fmt_section = QTextCharFormat()
        fmt_section.setForeground(QColor(ACCENT))
        fmt_section.setFontWeight(700)
        for pattern in ("1. INTRODUCTION", "2. SUGGESTIONS", "3. PROCEDURE",
                        "4. PURIFICATION", "5. PRODUIT ATTENDU"):
            cursor = doc.find(pattern)
            while not cursor.isNull():
                cursor.mergeCharFormat(fmt_section)
                cursor = doc.find(pattern, cursor)

        # --- Extraction automatique nom, MW et rendement ---
        txt = self._proc_content
        changed = False

        # Nom du produit : "Nom IUPAC : ..." en priorite, sinon "Nom courant : ..."
        name_match = re.search(r"^Nom IUPAC\s*:\s*(.+)$", txt, re.MULTILINE)
        if not name_match:
            name_match = re.search(r"^Nom courant\s*:\s*(.+)$", txt, re.MULTILINE)
        if name_match and not self._prod_name:
            self._prod_name = name_match.group(1).strip()
            changed = True

        # MW : "Masse molaire : 387,59 g/mol"
        mw_match = re.search(
            r"Masse molaire\s*:\s*([\d][0-9.,]*)\s*g/mol", txt, re.IGNORECASE
        )
        if mw_match and not self._prod_mw_manual and self.reagents:
            try:
                mw_val = float(mw_match.group(1).replace(",", "."))
                self._prod_mw = mw_val
                self._prod_mw_manual = True
                changed = True
            except ValueError:
                pass

        # Rendement : "Rendement typique estime : 40-60 %" → prend la valeur haute
        yld_match = re.search(
            r"Rendement typique estim[eé]\s*:\s*([\d.]+)\s*[-–àa]\s*([\d.]+)\s*%",
            txt, re.IGNORECASE
        )
        if yld_match and not self._prod_yield_manual and self.reagents:
            try:
                yld_hi = max(float(yld_match.group(1)), float(yld_match.group(2)))
                self._prod_yield = yld_hi / 100.0
                self._prod_yield_manual = True
                changed = True
            except ValueError:
                pass
        elif not yld_match:
            # Format valeur unique : "Rendement typique estime : 65 %"
            yld_single = re.search(
                r"Rendement typique estim[eé]\s*:\s*([\d.]+)\s*%", txt, re.IGNORECASE
            )
            if yld_single and not self._prod_yield_manual and self.reagents:
                try:
                    self._prod_yield = float(yld_single.group(1)) / 100.0
                    self._prod_yield_manual = True
                    changed = True
                except ValueError:
                    pass

        # Solvant : "Solvant : acétone" / "Solvants recommandés : THF/eau"
        if not self._rxn_solvant:
            solv_match = re.search(
                r"Solvant[s]?\s*(?:recommand[eé]s?)?\s*:\s*([^\n\.]+)",
                txt, re.IGNORECASE
            )
            if solv_match:
                self._rxn_solvant = solv_match.group(1).strip().rstrip(",")
                changed = True

        # Température : label "Température : 70°C" ou narratif "à 30-40°C" / "à 60 °C"
        if not self._rxn_temp:
            temp_match = re.search(
                r"Temp[eé]rature\s*(?:[\w\s]*):\s*([\d]+)\s*°?\s*C\b",
                txt, re.IGNORECASE
            )
            if temp_match:
                self._rxn_temp = temp_match.group(1).strip()
                changed = True
            else:
                # plage "à 30-40°C" → prend la valeur haute
                temp_range = re.search(r"\b[àa]\s*([\d]+)\s*[-–]\s*([\d]+)\s*°\s*C\b", txt, re.IGNORECASE)
                if temp_range:
                    self._rxn_temp = str(max(int(temp_range.group(1)), int(temp_range.group(2))))
                    changed = True
                else:
                    temp_single = re.search(r"\b[àa]\s*([\d]+)\s*°\s*C\b", txt, re.IGNORECASE)
                    if temp_single:
                        self._rxn_temp = temp_single.group(1).strip()
                        changed = True

        # Durée / Temps : label "Durée : 2 h" ou narratif "pendant 2-4 heures" / "pendant 1 h"
        if not self._rxn_time:
            time_match = re.search(
                r"(?:Dur[eé]e|Temps)\s*(?:[\w\s]*):\s*([\d.,]+)\s*h\b",
                txt, re.IGNORECASE
            )
            if time_match:
                self._rxn_time = time_match.group(1).replace(",", ".").strip()
                changed = True
            else:
                # plage "pendant 2-4 heures" → prend la valeur haute
                time_range = re.search(
                    r"pendant\s+([\d]+)\s*[-–]\s*([\d]+)\s*h(?:eure)?s?\b",
                    txt, re.IGNORECASE
                )
                if time_range:
                    self._rxn_time = str(max(int(time_range.group(1)), int(time_range.group(2))))
                    changed = True
                else:
                    time_single = re.search(
                        r"pendant\s+([\d.,]+)\s*h(?:eure)?s?\b",
                        txt, re.IGNORECASE
                    )
                    if time_single:
                        self._rxn_time = time_single.group(1).replace(",", ".").strip()
                        changed = True

        if changed:
            self._rebuild_table()

    # =========================================================================
    # Chat IA
    # =========================================================================
    def _append_chat_text(self, text: str):
        cursor = self._chat_txt.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._chat_txt.setTextCursor(cursor)
        self._chat_txt.ensureCursorVisible()

    def _chat_response_done(self):
        self._chat_typing = False
        self._chat_input.setEnabled(True)
        self._append_chat_text("\n")

    def _send_chat_message(self):
        question = self._chat_input.text().strip()
        if not question or self._chat_typing:
            return
        if not self._proc_content:
            QMessageBox.information(self, "Info", "Generez d'abord une procedure IA.")
            return
        api_key, provider = self._get_api_key()
        if not api_key:
            return

        self._chat_input.clear()
        self._chat_input.setEnabled(False)
        self._chat_typing = True

        self._append_chat_text(f"Vous : {question}\n")
        self._append_chat_text("IA : ")

        self._chat_history.append({"role": "user", "content": question})

        context = (
            "Voici la fiche de synthese qui a ete generee pour cette reaction :\n\n"
            + self._proc_content
            + "\n\nReponds aux questions de l'utilisateur en te basant sur cette fiche "
            "et tes connaissances en chimie organique. Sois concis et pratique."
        )

        history = self._chat_history

        def run():
            try:
                full_response = ""
                if provider == "gemini":
                    if not _GEMINI_OK:
                        self.sig_chat_append.emit("Erreur : google-genai non installe.")
                        self.sig_chat_done.emit()
                        return
                    client = genai_lib.Client(api_key=api_key)
                    messages = [{"role": "user", "parts": [{"text": context}]},
                                {"role": "model", "parts": [{"text": "Compris, je suis pret a repondre a vos questions."}]}]
                    for msg in history[:-1]:
                        role = "user" if msg["role"] == "user" else "model"
                        messages.append({"role": role, "parts": [{"text": msg["content"]}]})
                    messages.append({"role": "user", "parts": [{"text": history[-1]["content"]}]})
                    stream = client.models.generate_content_stream(
                        model="gemini-2.5-flash",
                        contents=messages,
                        config=genai_lib.types.GenerateContentConfig(
                            system_instruction=system_msg_chat,
                            thinking_config=genai_lib.types.ThinkingConfig(thinking_budget=0),
                        ),
                    )
                    for chunk in stream:
                        try:
                            text = chunk.text or ""
                        except Exception:
                            text = ""
                        if text:
                            full_response += text
                            self.sig_chat_append.emit(text)
                else:
                    client = groq_lib.Groq(api_key=api_key)
                    messages = [
                        {"role": "system", "content": system_msg_chat + "\n\n" + context},
                    ]
                    for msg in history:
                        messages.append({"role": msg["role"], "content": msg["content"]})
                    stream = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        max_tokens=512,
                        messages=messages,
                        stream=True,
                    )
                    for chunk in stream:
                        text = chunk.choices[0].delta.content or ""
                        if text:
                            full_response += text
                            self.sig_chat_append.emit(text)
                self._chat_history.append({"role": "assistant", "content": full_response})
                self.sig_chat_done.emit()
            except Exception as e:
                self.sig_chat_append.emit(f"\nErreur : {e}")
                self.sig_chat_done.emit()

        system_msg_chat = (
            "Tu es un chimiste expert en synthese organique. "
            "Tu assistes l'utilisateur en repondant a ses questions sur une procedure de synthese. "
            "Sois concis, pratique et precis. Reponds en francais."
        )
        threading.Thread(target=run, daemon=True).start()

    # =========================================================================
    # API Groq / Procedure
    # =========================================================================
    def _get_api_key(self) -> tuple[str, str] | tuple[None, None]:
        provider = charger_provider()
        key = charger_api_key(provider)
        if not key:
            key = self._set_api_key_dialog()
            provider = charger_provider()
        return (key, provider) if key else (None, None)

    def _set_api_key_dialog(self) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Fournisseur IA & Cles API")
        dlg.setFixedWidth(460)
        dlg.setStyleSheet(f"background: {BG};")
        vl = QVBoxLayout(dlg)
        vl.setSpacing(10)
        vl.setContentsMargins(20, 16, 20, 16)

        # Sélecteur fournisseur
        vl.addWidget(QLabel("Fournisseur IA :"))
        combo = QComboBox()
        combo.addItems(["gemini", "groq"])
        combo.setCurrentText(charger_provider())
        combo.setFixedHeight(32)
        vl.addWidget(combo)

        # Gemini
        lbl_gem = QLabel("Cle API Gemini (AIza...) :")
        vl.addWidget(lbl_gem)
        hint_gem = QLabel("Cle gratuite sur aistudio.google.com")
        hint_gem.setStyleSheet(f"color: {DIM}; font-size: 9px;")
        vl.addWidget(hint_gem)
        entry_gem = QLineEdit()
        entry_gem.setEchoMode(QLineEdit.EchoMode.Password)
        entry_gem.setFixedHeight(32)
        entry_gem.setText(charger_api_key("gemini"))
        vl.addWidget(entry_gem)

        # Groq
        lbl_groq = QLabel("Cle API Groq (gsk_...) :")
        vl.addWidget(lbl_groq)
        hint_groq = QLabel("Cle gratuite sur console.groq.com")
        hint_groq.setStyleSheet(f"color: {DIM}; font-size: 9px;")
        vl.addWidget(hint_groq)
        entry_groq = QLineEdit()
        entry_groq.setEchoMode(QLineEdit.EchoMode.Password)
        entry_groq.setFixedHeight(32)
        entry_groq.setText(charger_api_key("groq"))
        vl.addWidget(entry_groq)

        btn = QPushButton("Valider")
        btn.setFixedHeight(32)
        btn.setStyleSheet(btn_style())
        result = [None]

        def valider():
            provider = combo.currentText()
            kg = entry_gem.text().strip()
            kq = entry_groq.text().strip()
            _sauvegarder_config(ai_provider=provider, gemini_key=kg, groq_key=kq)
            result[0] = kg if provider == "gemini" else kq
            dlg.accept()

        btn.clicked.connect(valider)
        vl.addWidget(btn)
        dlg.exec()
        return result[0]

    def _about(self):
        QMessageBox.about(
            self, "A propos",
            "Calculateur de Stochiometrie\n"
            "PyQt6 + Gemini 2.0 Flash / Groq (llama-3.3-70b) + PubChem\n\n"
            "H&B — Synthese organique"
        )

    # =========================================================================
    # Paramètres d'affichage
    # =========================================================================
    def _load_display_settings(self) -> dict:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            ds = dict(DEFAULT_DISPLAY_SETTINGS)
            ds.update(data.get("display", {}))
            return ds
        except Exception:
            return dict(DEFAULT_DISPLAY_SETTINGS)

    def _save_display_settings(self):
        try:
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            data["display"] = self._ds
            CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _open_display_settings(self):
        dlg = DisplaySettingsDialog(self._ds, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._ds = dlg.get_settings()
            self._save_display_settings()
            self._apply_display_settings()

    def _apply_display_settings(self):
        name_val = self.f_name.text() if hasattr(self, "f_name") else ""
        mw_val   = self.f_mw.text()   if hasattr(self, "f_mw")   else ""
        self._build_form_fields()
        if name_val: self.f_name.setText(name_val)
        if mw_val:   self.f_mw.setText(mw_val)
        if self.reagents:
            self._rebuild_table()

    def _generer_procedure(self):
        if not self.reagents:
            QMessageBox.information(self, "Info", "Ajoutez d'abord des reactifs."); return
        lim = next((r for r in self.reagents if r["role"] == "Limitant"), None)
        if not lim:
            QMessageBox.information(self, "Info", "Definissez un reactif Limitant."); return
        api_key, provider = self._get_api_key()
        if not api_key: return

        self.sig_clear.emit()
        self._append_proc_text("Generation en cours...\n")
        self._show_proc_panel()

        # Construire le prompt
        n_lim_mol = None
        if lim["mw"] and lim["mass_g"]:
            n_lim_mol = lim["mass_g"] * (lim["purity"] / 100.0) / lim["mw"]

        lignes = []
        for r in self.reagents:
            role = r["role"]; nom = r["name"]; mw = r["mw"]
            pur  = r["purity"]; eq = r["eq"]; mass = r["mass_g"]; dens = r["density"]
            if role == "Limitant":
                mol = n_lim_mol
                masse_txt = f"{mass:.3f} g" if mass else ""
            else:
                mol = eq * n_lim_mol if n_lim_mol else None
                masse_calc = mol * mw / (pur / 100.0) if (mol and mw and pur > 0) else None
                if dens and masse_calc:
                    masse_txt = f"{masse_calc:.3f} g ({masse_calc/dens:.2f} mL)"
                elif masse_calc:
                    masse_txt = f"{masse_calc:.3f} g"
                else:
                    masse_txt = ""
            mol_txt = f"{mol*1000:.2f} mmol" if mol else ""
            eq_txt  = f", {eq:.2f} eq" if role != "Limitant" else ", 1 eq (limitant)"
            pur_txt = f", purete {pur:.0f}%" if pur < 100 else ""
            mw_txt  = f", MW={mw:.2f} g/mol" if mw else ""
            ligne   = f"- [{role}] {nom}{mw_txt}{pur_txt}{eq_txt}"
            if masse_txt: ligne += f" -> {masse_txt}"
            if mol_txt:   ligne += f" ({mol_txt})"
            lignes.append(ligne)

        prod_col = len(self.reagents)
        w_mw = self._cells.get((prod_col, "mw"))
        prod_mw = _f(w_mw.text()) if w_mw else 0.0
        prod_mw_txt = f"MW estimee du produit : {prod_mw:.2f} g/mol\n" if prod_mw else ""

        cond_parts = []
        if self._rxn_solvant: cond_parts.append(f"Solvant : {self._rxn_solvant}")
        if self._rxn_temp:    cond_parts.append(f"Temperature : {self._rxn_temp} C")
        if self._rxn_time:    cond_parts.append(f"Duree : {self._rxn_time} h")
        conditions_txt = (
            "Conditions renseignees par l'utilisateur : " + ", ".join(cond_parts) + "\n"
        ) if cond_parts else ""

        system_msg = (
            "Tu es un chimiste expert en synthese organique avec une maitrise rigoureuse des mecanismes reactionnels. "
            "Avant de predire le produit d'une reaction, tu DOIS :\n"
            "1. Identifier TOUS les groupements fonctionnels de chaque reactif (amine, thiol SH, alcool OH, "
            "acide carboxylique COOH, isocyanate NCO, epoxyde, halogene, etc.).\n"
            "2. Appliquer l'ordre de reactivite correct base sur la nucleophilie et la selectivite connue.\n"
            "3. Determiner quel groupement reagit EN PREMIER selon cet ordre, et construire le produit en consequence.\n\n"
            "Regles de reactivite importantes (a respecter absolument) :\n"
            "- Avec un ISOCYANATE (R-N=C=O) : ordre de reactivite des nucleophiles = "
            "amine primaire > amine secondaire > thiol (SH) >> eau > alcool > acide carboxylique. "
            "Un thiol presente avec un isocyanate donne un S-THIOCARBAMATE (liaison C(=O)-S, "
            "c'est-a-dire R-NH-C(=O)-S-R'), PAS un O-thiocarbamate (C(=S)-O) ni un amide. "
            "Ne jamais confondre ces trois structures.\n"
            "- Avec un EPOXYDE : les thiols et amines sont plus reactifs que les alcools.\n"
            "- Avec un ANHYDRIDE : les amines reagissent avant les alcools.\n"
            "Si un reactif possede PLUSIEURS groupements fonctionnels, indique lequel reagit "
            "et pourquoi, avant de nommer le produit.\n\n"
            "Regles de nomenclature IUPAC (a respecter absolument) :\n"
            "- Le nom IUPAC doit etre EN ANGLAIS (c'est la langue officielle de l'IUPAC). "
            "Exemples : 'mercaptoacetic' et non 'mercaptoacetique', 'thiocarbamate' reste tel quel, "
            "'octadecyl' et non 'octadecyle'.\n"
            "- Pour les thiocarbamates issus de thiol + isocyanate (S-thiocarbamate) : "
            "utiliser le prefixe 'S-' pour lever l'ambiguite, ou la formulation "
            "'[(alkylcarbamoyl)thio]' pour le fragment -NH-C(=O)-S- dans le nom substitutif.\n"
            "- Verifier la coherence entre la structure dessinee mentalement et le nom genere.\n\n"
            "RIGUEUR MECANIQUE OBLIGATOIRE avant tout nommage :\n"
            "1. Compter les carbones de chaque reactif (ex: acide thioglycolique = C2, octadecyl isocyanate = C19).\n"
            "2. Identifier si le mecanisme est une Addition (somme des carbones), une Elimination/Fragmentation "
            "(perte de CO2, MeOH, etc.) ou une Substitution (remplacement d'un fragment).\n"
            "3. Calculer le bilan carbone explicite : C(reactif1) + C(reactif2) - C(pertes) = C(produit).\n"
            "4. Le nom IUPAC genere DOIT correspondre exactement a ce bilan. "
            "Il est INTERDIT d'utiliser un nom standard qui ne correspond pas au bilan carbone calcule. "
            "Si le squelette conserve un CH2, le nom doit le refleter exactement — ne jamais l'omettre ou l'alterer par habitude."
        )

        prompt = (
            "Voici les reactifs d'une reaction :\n"
            + "\n".join(lignes) + "\n"
            + prod_mw_txt + conditions_txt
            + """
Genere une fiche de synthese structuree EN FRANCAIS, en respectant EXACTEMENT ce format (5 sections) :

1. INTRODUCTION
Type de reaction : [type precis, ex : Substitution nucleophile SN2, Esterification de Fischer, formation de thiocarbamate, etc.]
Groupements fonctionnels impliques : [lister les GF de chaque reactif, puis indiquer lequel reagit avec lequel et pourquoi]
Bilan carbone : [ex: C19 (isocyanate) + C2 (acide thioglycolique) - 0 perte = C21 produit]
Nom potentiel du produit : [nom IUPAC ou courant CORRECT base sur la reactivite reelle ET le bilan carbone]
Conditions typiques pour ce type de reaction : [decris brievement les conditions classiquement utilisees pour ce mecanisme : solvant usuel, base ou acide typique, temperature standard, et cite un exemple connu de cette reaction si possible]

2. SUGGESTIONS
- Catalyseur recommande : [si applicable, sinon "aucun"]
- Solvant recommande : [solvant + volume approximatif]
- Atmosphere : [air / argon / azote selon sensibilite]
- Analyse des equivalents : [commenter si les equivalents fournis sont coherents avec la reaction]
- Reactifs potentiellement manquants : [lister tout ce qui semble absent et necessaire]
- Autres conseils : [remarques pratiques importantes]

3. PROCEDURE
-. [ex : Peser X mg de reactif A dans un ballon sec de Y mL]
-. [ex : Ajouter Z mL de solvant puis le reactif B sous agitation]
-. [ex : Chauffer a XX C pendant X h sous reflux]
-. Suivi par CCM (eluant recommande : [eluant coherent avec les reactifs])

4. PURIFICATION
-. Quencher avec [solution appropriee]
-. Extraire avec [solvant], X fois
-. Laver la phase organique avec [lavages]
-. Secher sur [agent dessechant], filtrer, evaporer sous pression reduite
-. Purifier par [colonne / recristallisation / distillation] : [conditions detaillees]

5. PRODUIT ATTENDU
Nom IUPAC : [nom IUPAC EN ANGLAIS, complet et rigoureux]
Nom courant : [nom usuel si applicable, sinon "aucun"]
Masse molaire : [X] g/mol
Rendement typique estime : [X-X] %
Remarques : [etat physique, couleur, stabilite, conservation]

Ne genere RIEN d'autre que ces 5 sections numerotees. Sois concis et pratique."""
        )

        def run():
            try:
                self.sig_clear.emit()
                if provider == "gemini":
                    if not _GEMINI_OK:
                        self.sig_append.emit("Erreur : installez google-genai (pip install google-genai)")
                        return
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
                            self.sig_append.emit(text)
                else:
                    client = groq_lib.Groq(api_key=api_key)
                    stream = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        max_tokens=1024,
                        messages=[
                            {"role": "system", "content": system_msg},
                            {"role": "user", "content": prompt},
                        ],
                        stream=True,
                    )
                    for chunk in stream:
                        text = chunk.choices[0].delta.content or ""
                        if text:
                            self.sig_append.emit(text)
                self.sig_done.emit()
            except Exception as e:
                self.sig_append.emit(f"\n\nErreur : {e}")

        threading.Thread(target=run, daemon=True).start()

    # =========================================================================
    # Exports
    # =========================================================================
    def _get_matrix(self):
        n = len(self.reagents)
        if n == 0: return [], [], []
        col_names  = [r["name"] for r in self.reagents] + ["Produit"]
        row_labels = [label for label, *_ in ROWS]
        matrix = []
        for _, key, *_ in ROWS:
            row = [self._cells[(i, key)].text() or "-" for i in range(n)]
            row.append(self._cells[(n, key)].text() or "-")
            matrix.append(row)
        return col_names, row_labels, matrix

    def _export_csv(self):
        col_names, row_labels, matrix = self._get_matrix()
        if not col_names:
            QMessageBox.information(self, "Info", "Aucune donnee."); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Exporter CSV",
            f"{self.rxn_name_edit.text()}_stochio.csv",
            "CSV (*.csv)"
        )
        if not path: return
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(";" + ";".join(col_names) + "\n")
            for label, row in zip(row_labels, matrix):
                f.write(label + ";" + ";".join(row) + "\n")
            if self._proc_content:
                f.write("\n\nProcedure experimentale (IA)\n")
                f.write(self._proc_content + "\n")
        QMessageBox.information(self, "CSV", f"Enregistre :\n{path}")

    def _export_pdf(self):
        col_names, row_labels, matrix = self._get_matrix()
        if not col_names:
            QMessageBox.information(self, "Info", "Aucune donnee."); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Exporter PDF",
            f"{self.rxn_name_edit.text()}_stochio.pdf",
            "PDF (*.pdf)"
        )
        if not path: return
        with open(path, "wb") as f:
            f.write(make_pdf(
                col_names, row_labels, matrix,
                self.rxn_name_edit.text(),
                procedure=self._proc_content
            ))
        QMessageBox.information(self, "PDF", f"Enregistre :\n{path}")


# =============================================================================
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # Stylesheet global : garantit la lisibilité sur tous les OS/thèmes
    # Les stylesheets définis sur des widgets individuels prennent le dessus.
    app.setStyleSheet(f"""
        QLineEdit  {{ color: {TEXT}; background: white;
                      border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 6px; }}
        QLineEdit:focus {{ border-color: {ACCENT}; }}
        QLineEdit:read-only {{ background: {C_CALC}; color: {DIM}; }}
        QComboBox  {{ color: {TEXT}; background: white;
                      border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 6px; }}
        QComboBox QAbstractItemView {{ color: {TEXT}; background: white; }}
        QCheckBox  {{ color: {TEXT}; background: transparent; }}
        QSpinBox   {{ color: {TEXT}; background: white;
                      border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 6px; }}
        QTextEdit  {{ color: {TEXT}; background: white; }}
        QLabel     {{ color: {TEXT}; background: transparent; }}
        QFontComboBox {{ color: {TEXT}; background: white;
                         border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 4px; }}
        QFontComboBox QAbstractItemView {{ color: {TEXT}; background: white; }}
    """)
    window = App()
    window.show()
    sys.exit(app.exec())
