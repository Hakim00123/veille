#!/usr/bin/env python3
"""Transcrit le dernier episode de Generation Do It Yourself et en extrait les outils cites.

Le flux RSS expose directement le MP3 (balise enclosure) : pas de dependance a YouTube,
pas de blocage d'IP. La transcription se fait localement avec faster-whisper.

Sortie dans data/gdiy/ :
  <id>.json            metadonnees + mentions d'outils avec leur contexte
  <id>-transcript.txt  transcription integrale
  latest.json          pointeur vers le dernier episode traite
"""
import json
import os
import re
import sys
import unicodedata
from xml.etree import ElementTree as ET

import requests

FLUX = "https://feeds.audiomeans.fr/feed/b4a5ee3a-9230-4f9f-988d-2ae156a2d5a9.xml"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "gdiy")
MODELE = os.environ.get("WHISPER_MODEL", "base")   # tiny = rapide, base = meilleurs noms propres
CONTEXTE = 260                                      # caracteres de part et d'autre d'une mention

ENTETES = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

# Outils frequemment cites par les entrepreneurs. La liste sert d'amorce :
# les motifs contextuels ci-dessous rattrapent ceux qui n'y figurent pas.
OUTILS = [
    "Notion", "Slack", "Figma", "Airtable", "Zapier", "Make", "n8n", "HubSpot", "Salesforce",
    "Pipedrive", "Intercom", "Zendesk", "Stripe", "Qonto", "Pennylane", "Payfit", "Silae",
    "Workday", "SuccessFactors", "Cegid", "Lucca", "Personio", "Deel", "Alan", "Swile",
    "Google Analytics", "Amplitude", "Mixpanel", "Metabase", "Looker", "Tableau", "Power BI",
    "Snowflake", "BigQuery", "dbt", "Segment", "Fivetran",
    "Asana", "Trello", "Monday", "Jira", "Linear", "ClickUp", "Basecamp",
    "Canva", "Webflow", "Framer", "Shopify", "WordPress", "Wix", "Squarespace",
    "Mailchimp", "Brevo", "Klaviyo", "Lemlist", "Apollo", "Clay", "PhantomBuster",
    "ChatGPT", "Claude", "Gemini", "Perplexity", "Midjourney", "Cursor", "Copilot",
    "Superhuman", "Missive", "Loom", "Miro", "Whimsical", "Obsidian", "Roam", "Craft",
    "Excel", "Google Sheets", "Airflow", "Retool", "Bubble", "Softr", "Zoom", "Teams",
    "Salesforce", "NetSuite", "SAP", "Oracle", "Qlik", "Alteryx", "Talend",
]

# Formulations qui introduisent generalement un outil
MOTIFS = [
    r"on (?:utilise|bosse (?:avec|sur)|tourne sur|est (?:sur|passe a))\s+([A-Z][\w\.\- ]{2,25})",
    r"j['e] ?(?:utilise|bosse (?:avec|sur))\s+([A-Z][\w\.\- ]{2,25})",
    r"notre (?:stack|outil|CRM|ERP|SIRH)\s+(?:c'est\s+)?([A-Z][\w\.\- ]{2,25})",
    r"we (?:use|run on|built (?:it )?on)\s+([A-Z][\w\.\- ]{2,25})",
    r"our (?:stack|tool|CRM|ERP)\s+(?:is\s+)?([A-Z][\w\.\- ]{2,25})",
]


def sans_accent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slugifier(s):
    s = sans_accent(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60]


def dernier_episode():
    """Retourne le dernier episode complet, en privilegiant la version francaise."""
    r = requests.get(FLUX, timeout=40, headers=ENTETES)
    r.raise_for_status()
    racine = ET.fromstring(r.content)

    complets = []
    for item in racine.iter("item"):
        titre = (item.findtext("title") or "").strip()
        if not re.match(r"^#\d+", titre):          # ecarte [SNIPPET] et [EXTRAIT]
            continue
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        complets.append({
            "titre": titre,
            "audio": enc.get("url"),
            "date": (item.findtext("pubDate") or "")[:16],
            "lien": (item.findtext("link") or "").strip(),
            "vf": " - VF - " in titre,
        })
        if len(complets) >= 6:
            break

    if not complets:
        return None
    # meme numero d'episode : on prefere la VF
    num = re.match(r"^#(\d+)", complets[0]["titre"]).group(1)
    memes = [e for e in complets if e["titre"].startswith("#" + num)]
    for e in memes:
        if e["vf"]:
            return e
    return memes[0]


def transcrire(chemin_audio):
    from faster_whisper import WhisperModel
    modele = WhisperModel(MODELE, device="cpu", compute_type="int8")
    segments, info = modele.transcribe(chemin_audio, language="fr", vad_filter=True)
    morceaux = []
    for s in segments:
        morceaux.append(f"[{int(s.start)//60:02d}:{int(s.start)%60:02d}] {s.text.strip()}")
    return "\n".join(morceaux), info.language


def reperer_outils(texte):
    """Retourne les mentions d'outils avec leur contexte, dedoublonnees."""
    plat = re.sub(r"\[\d+:\d+\]\s*", "", texte)
    plat = re.sub(r"\s+", " ", plat)
    trouves = {}

    for outil in OUTILS:
        for m in re.finditer(r"\b" + re.escape(outil) + r"\b", plat, re.IGNORECASE):
            d, f = max(0, m.start() - CONTEXTE), min(len(plat), m.end() + CONTEXTE)
            trouves.setdefault(outil, []).append(plat[d:f].strip())

    connus = {k.lower() for k in trouves}
    for motif in MOTIFS:
        for m in re.finditer(motif, plat):
            brut = m.group(1).strip().rstrip(".,;:")
            # ne garder que le nom propre : mots capitalises consecutifs, 2 au maximum
            mots = brut.split()
            nom = " ".join(mots[:2]) if len(mots) > 1 and mots[1][:1].isupper() else mots[0]
            nom = nom.strip().rstrip(".,;:")
            if len(nom) < 3 or nom.lower() in {"the", "les", "des", "une", "notre", "mais", "and"}:
                continue
            # ecarter ce qui est deja couvert par la liste d'outils connus
            if nom.lower() in connus or any(nom.lower().startswith(c) for c in connus):
                continue
            if nom not in trouves:
                d, f = max(0, m.start() - CONTEXTE), min(len(plat), m.end() + CONTEXTE)
                trouves.setdefault(nom, []).append(plat[d:f].strip())

    # au plus 3 extraits par outil, pour garder le fichier lisible
    return {k: v[:3] for k, v in sorted(trouves.items())}


def main():
    ep = dernier_episode()
    if not ep:
        print("Aucun episode complet trouve dans le flux.")
        return

    os.makedirs(OUT, exist_ok=True)
    ident = slugifier(ep["titre"])
    fiche = os.path.join(OUT, ident + ".json")

    if os.path.exists(fiche):
        print(f"Deja traite : {ep['titre']}")
        return

    print(f"Episode : {ep['titre']}")
    print(f"Telechargement : {ep['audio']}")
    audio = "/tmp/gdiy.mp3"
    with requests.get(ep["audio"], stream=True, timeout=180, headers=ENTETES) as r:
        r.raise_for_status()
        with open(audio, "wb") as fh:
            for bloc in r.iter_content(1 << 20):
                fh.write(bloc)
    print(f"Telecharge : {os.path.getsize(audio) // 1048576} Mo")

    print(f"Transcription (modele {MODELE})...")
    texte, langue = transcrire(audio)
    print(f"Transcription terminee : {len(texte)} caracteres, langue detectee {langue}")

    with open(os.path.join(OUT, ident + "-transcript.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"{ep['titre']}\n{ep['date']}\n{ep['lien']}\n\n{texte}")

    outils = reperer_outils(texte)
    fiche_data = {
        "titre": ep["titre"],
        "date": ep["date"],
        "lien": ep["lien"],
        "modele": MODELE,
        "langue": langue,
        "taille_transcript": len(texte),
        "transcript": f"{ident}-transcript.txt",
        "nb_outils": len(outils),
        "outils": outils,
    }
    with open(fiche, "w", encoding="utf-8") as fh:
        json.dump(fiche_data, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump({"fiche": ident + ".json", "titre": ep["titre"], "date": ep["date"]},
                  fh, ensure_ascii=False, indent=1)

    os.remove(audio)
    print(f"{len(outils)} outils reperes : {', '.join(list(outils)[:12])}")


if __name__ == "__main__":
    main()
