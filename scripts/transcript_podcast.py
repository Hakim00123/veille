#!/usr/bin/env python3
"""Transcrit les nouveaux episodes des podcasts listes dans podcasts.json.

Le MP3 vient de la balise <enclosure> du flux RSS : pas de dependance a YouTube,
pas de blocage d'IP, pas de compte. La transcription est locale (faster-whisper).

Usage :
  python scripts/transcript_podcast.py --matrice        # matrice GitHub Actions
  python scripts/transcript_podcast.py --slug a16z      # transcrit un podcast
  python scripts/transcript_podcast.py --slug a16z --a-blanc   # sans transcrire

Sortie dans data/podcasts/<slug>/ :
  <id>.json            fiche : metadonnees + outils cites avec leur contexte
  <id>-transcript.txt  transcription integrale horodatee
  index.json           episodes deja traites + pointeur vers le plus recent
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "podcasts.json")
OUT = os.path.join(ROOT, "data", "podcasts")
CONTEXTE = 260          # caracteres de part et d'autre d'une mention
TAILLE_MAX_MO = 400     # au-dela, l'episode est ignore (evite les rediffusions fleuves)

ENTETES = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

# Amorce : outils frequemment cites. Les motifs contextuels rattrapent les autres.
OUTILS = [
    "Notion", "Slack", "Figma", "Airtable", "Zapier", "Make", "n8n", "HubSpot", "Salesforce",
    "Pipedrive", "Intercom", "Zendesk", "Stripe", "Qonto", "Pennylane", "Payfit", "Silae",
    "Workday", "SuccessFactors", "Cegid", "Lucca", "Personio", "Deel", "Alan", "Swile",
    "Rippling", "Gusto", "Justworks", "BambooHR", "Greenhouse", "Lever", "Ashby",
    "Google Analytics", "Amplitude", "Mixpanel", "Metabase", "Looker", "Tableau", "Power BI",
    "Snowflake", "BigQuery", "Databricks", "dbt", "Segment", "Fivetran", "Airbyte", "Hex",
    "Asana", "Trello", "Monday", "Jira", "Linear", "ClickUp", "Basecamp", "Height", "Shortcut",
    "Canva", "Webflow", "Framer", "Shopify", "WordPress", "Wix", "Squarespace", "Ghost",
    "Mailchimp", "Brevo", "Klaviyo", "Lemlist", "Apollo", "Clay", "PhantomBuster", "Instantly",
    "ChatGPT", "Claude", "Claude Code", "Gemini", "Perplexity", "Midjourney", "Cursor",
    "Copilot", "Codex", "Devin", "Windsurf", "Replit", "Lovable", "Bolt", "v0",
    "LangChain", "LlamaIndex", "Pinecone", "Weaviate", "Chroma", "Ollama", "vLLM",
    "Hugging Face", "Modal", "Together", "Fireworks", "Groq", "Baseten", "LangSmith",
    "Superhuman", "Missive", "Loom", "Miro", "Whimsical", "Obsidian", "Roam", "Craft",
    "Excel", "Google Sheets", "Airflow", "Dagster", "Retool", "Bubble", "Softr",
    "Zoom", "Teams", "Discord", "Granola", "Otter", "Descript", "Riverside",
    "NetSuite", "SAP", "Oracle", "Qlik", "Alteryx", "Talend", "Ramp", "Brex", "Mercury",
    "Vercel", "Netlify", "Supabase", "Firebase", "Render", "Fly.io", "Railway",
    "AWS", "GCP", "Azure", "Cloudflare", "Datadog", "Sentry", "PostHog", "Stytch",
]

# Formulations qui introduisent generalement un outil
MOTIFS = [
    r"on (?:utilise|bosse (?:avec|sur)|tourne sur|est (?:sur|passe a))\s+([A-Z][\w\.\- ]{2,25})",
    r"j['e] ?(?:utilise|bosse (?:avec|sur))\s+([A-Z][\w\.\- ]{2,25})",
    r"notre (?:stack|outil|CRM|ERP|SIRH)\s+(?:c'est\s+)?([A-Z][\w\.\- ]{2,25})",
    r"we (?:use|used|run on|rely on|built (?:it )?on|switched to|moved to)\s+([A-Z][\w\.\- ]{2,25})",
    r"our (?:whole )?(?:stack|tool|tooling|CRM|ERP|setup)\s+(?:is\s+)?([A-Z][\w\.\- ]{2,25})",
    r"I (?:use|used|live in|run everything (?:in|on))\s+([A-Z][\w\.\- ]{2,25})",
    r"(?:built|building) (?:it |this )?(?:on|with|in)\s+([A-Z][\w\.\- ]{2,25})",
]

VIDE = {"the", "les", "des", "une", "notre", "mais", "and", "that", "this", "these", "those",
        "our", "your", "their", "it", "we", "you", "they", "there", "what", "when", "which",
        "for", "with", "from", "about", "into", "some", "any", "all", "one", "two"}


def sans_accent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slugifier(s):
    s = sans_accent(s).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:70]


def charger_config():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def podcast_par_slug(cfg, slug):
    for p in cfg["podcasts"]:
        if p["slug"] == slug:
            return p
    raise SystemExit(f"Slug inconnu : {slug}. Connus : {[p['slug'] for p in cfg['podcasts']]}")


def lire_index(slug):
    chemin = os.path.join(OUT, slug, "index.json")
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass
    return {"slug": slug, "traites": [], "episodes": []}


def ecrire_index(slug, index):
    os.makedirs(os.path.join(OUT, slug), exist_ok=True)
    index["episodes"] = sorted(index["episodes"], key=lambda e: e.get("date_iso", ""), reverse=True)[:40]
    index["traites"] = index["traites"][-200:]
    index["maj"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(OUT, slug, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)


def date_item(item):
    brut = item.findtext("pubDate") or ""
    try:
        d = parsedate_to_datetime(brut)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def episodes_candidats(pod, fenetre_jours):
    """Episodes publies dans la fenetre, avec un MP3, non filtres par le podcast."""
    r = requests.get(pod["flux"], timeout=60, headers=ENTETES)
    r.raise_for_status()
    racine = ET.fromstring(r.content)
    limite = datetime.now(timezone.utc) - timedelta(days=fenetre_jours)
    motif_titre = pod.get("titre_valide")

    trouves = []
    for item in racine.iter("item"):
        titre = (item.findtext("title") or "").strip()
        if not titre:
            continue
        if motif_titre and not re.match(motif_titre, titre):
            continue
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url or ".mp3" not in url.lower() and ".m4a" not in url.lower():
            continue
        d = date_item(item)
        if d is None or d < limite:
            continue
        trouves.append({
            "titre": titre,
            "audio": url,
            "date": d.strftime("%a, %d %b %Y"),
            "date_iso": d.strftime("%Y-%m-%d"),
            "lien": (item.findtext("link") or "").strip(),
            "duree": (item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration") or "").strip(),
        })

    # doublons VO/VF : garder la version preferee (GDIY publie les deux)
    preferer = pod.get("preferer")
    if preferer:
        par_cle, ordre = {}, []
        for e in trouves:
            cle = re.match(r"^#\d+", e["titre"])
            cle = cle.group(0) if cle else e["titre"]
            if cle not in par_cle:
                par_cle[cle], _ = e, ordre.append(cle)
            elif preferer in e["titre"] and preferer not in par_cle[cle]["titre"]:
                par_cle[cle] = e
        trouves = [par_cle[c] for c in ordre]

    return sorted(trouves, key=lambda e: e["date_iso"], reverse=True)


def transcrire(chemin_audio, modele, langue):
    from faster_whisper import WhisperModel
    m = WhisperModel(modele, device="cpu", compute_type="int8")
    segments, info = m.transcribe(chemin_audio, language=langue, vad_filter=True)
    morceaux = [f"[{int(s.start)//60:02d}:{int(s.start)%60:02d}] {s.text.strip()}" for s in segments]
    return "\n".join(morceaux), info.language


def reperer_outils(texte):
    """Mentions d'outils avec leur contexte, dedoublonnees. Bruyant par nature :
    la synthese doit confirmer chaque mention en lisant le contexte."""
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
            mots = brut.split()
            nom = " ".join(mots[:2]) if len(mots) > 1 and mots[1][:1].isupper() else mots[0]
            nom = nom.strip().rstrip(".,;:")
            if len(nom) < 3 or nom.lower() in VIDE:
                continue
            if nom.lower() in connus or any(nom.lower().startswith(c) for c in connus):
                continue
            d, f = max(0, m.start() - CONTEXTE), min(len(plat), m.end() + CONTEXTE)
            trouves.setdefault(nom, []).append(plat[d:f].strip())

    return {k: v[:3] for k, v in sorted(trouves.items())}


def traiter(pod, ep, cfg):
    ident = slugifier(ep["titre"])
    dossier = os.path.join(OUT, pod["slug"])
    os.makedirs(dossier, exist_ok=True)
    audio = f"/tmp/{pod['slug']}.mp3"

    print(f"  telechargement : {ep['audio'][:90]}")
    with requests.get(ep["audio"], stream=True, timeout=300, headers=ENTETES) as r:
        r.raise_for_status()
        with open(audio, "wb") as fh:
            for bloc in r.iter_content(1 << 20):
                fh.write(bloc)
    taille = os.path.getsize(audio) // 1048576
    print(f"  telecharge : {taille} Mo")
    if taille > TAILLE_MAX_MO:
        os.remove(audio)
        raise RuntimeError(f"episode trop long ({taille} Mo > {TAILLE_MAX_MO})")

    depart = time.time()
    texte, langue = transcrire(audio, pod.get("modele", "base"), pod.get("langue"))
    minutes = (time.time() - depart) / 60
    print(f"  transcrit en {minutes:.0f} min : {len(texte)} caracteres")
    os.remove(audio)

    with open(os.path.join(dossier, ident + "-transcript.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"{pod['nom']}\n{ep['titre']}\n{ep['date']}\n{ep['lien']}\n\n{texte}")

    outils = reperer_outils(texte)
    fiche = {
        "podcast": pod["nom"], "slug": pod["slug"],
        "titre": ep["titre"], "date": ep["date"], "date_iso": ep["date_iso"],
        "lien": ep["lien"], "duree": ep["duree"],
        "modele": pod.get("modele", "base"), "langue": langue,
        "minutes_transcription": round(minutes, 1),
        "taille_transcript": len(texte),
        "transcript": ident + "-transcript.txt",
        "nb_outils": len(outils),
        "outils": outils,
    }
    with open(os.path.join(dossier, ident + ".json"), "w", encoding="utf-8") as fh:
        json.dump(fiche, fh, ensure_ascii=False, indent=1)
    return ident, fiche


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--matrice", action="store_true", help="ecrit la matrice GitHub Actions")
    ap.add_argument("--a-blanc", action="store_true", help="liste sans telecharger ni transcrire")
    args = ap.parse_args()
    cfg = charger_config()

    if args.matrice:
        matrice = {"include": [{"slug": p["slug"], "nom": p["nom"]} for p in cfg["podcasts"]]}
        sortie = os.environ.get("GITHUB_OUTPUT")
        ligne = "matrice=" + json.dumps(matrice)
        if sortie:
            with open(sortie, "a") as fh:
                fh.write(ligne + "\n")
        print(ligne)
        return

    if not args.slug:
        raise SystemExit("--slug ou --matrice requis")

    pod = podcast_par_slug(cfg, args.slug)
    index = lire_index(pod["slug"])
    deja = set(index["traites"])

    try:
        candidats = episodes_candidats(pod, cfg.get("fenetre_jours", 8))
    except Exception as e:                                   # flux mort ou illisible
        print(f"[{pod['slug']}] flux illisible : {type(e).__name__} {e}")
        return

    nouveaux = [e for e in candidats if slugifier(e["titre"]) not in deja]
    plafond = cfg.get("max_par_podcast", 3)
    retenus = nouveaux[:plafond]

    print(f"[{pod['slug']}] {len(candidats)} episode(s) dans la fenetre, "
          f"{len(nouveaux)} nouveau(x), {len(retenus)} retenu(s)")
    for e in retenus:
        print(f"  - {e['date_iso']}  {e['duree'] or '?':>8}  {e['titre'][:70]}")
    if args.a_blanc or not retenus:
        return

    echecs = 0
    for ep in retenus:
        try:
            ident, fiche = traiter(pod, ep, cfg)
            index["traites"].append(ident)
            index["episodes"].append({
                "fiche": ident + ".json", "titre": ep["titre"],
                "date": ep["date"], "date_iso": ep["date_iso"],
                "nb_outils": fiche["nb_outils"],
            })
            ecrire_index(pod["slug"], index)      # ecrit au fil de l'eau
        except Exception as e:
            echecs += 1
            print(f"  echec sur « {ep['titre'][:60]} » : {type(e).__name__} {e}", file=sys.stderr)

    print(f"[{pod['slug']}] termine, {len(retenus) - echecs} fiche(s) ecrite(s), {echecs} echec(s)")


if __name__ == "__main__":
    main()
