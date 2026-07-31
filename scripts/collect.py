#!/usr/bin/env python3
"""Collecte tous les flux listes dans feeds.opml et ecrit un instantane glissant.

Sortie : data/latest/index.json + data/latest/chunk-NN.json
Les chunks sont volontairement petits pour rester lisibles d'un seul tenant.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPML = os.path.join(ROOT, "feeds.opml")
OUT = os.path.join(ROOT, "data", "latest")

FENETRE_JOURS = 30      # profondeur d'historique conservee
TAILLE_CHUNK = 60       # articles par fichier
TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ENTETES = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def flux_depuis_opml(chemin):
    arbre = ET.parse(chemin)
    flux = []
    for o in arbre.iter("outline"):
        url = o.get("xmlUrl")
        if url:
            flux.append((o.get("text") or o.get("title") or url, url))
    return flux


def recuperer(item):
    nom, url = item
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=ENTETES)
        r.raise_for_status()
        return nom, url, feedparser.parse(r.content).entries, None
    except Exception as e:
        return nom, url, [], f"{type(e).__name__}: {e}"[:200]


def date_entree(e):
    for cle in ("published_parsed", "updated_parsed"):
        v = e.get(cle)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def nettoyer(html, n):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()[:n]


def main():
    flux = flux_depuis_opml(OPML)
    limite = datetime.now(timezone.utc) - timedelta(days=FENETRE_JOURS)
    articles, erreurs = [], []

    with ThreadPoolExecutor(max_workers=12) as pool:
        for nom, url, entrees, err in pool.map(recuperer, flux):
            if err:
                erreurs.append({"source": nom, "url": url, "erreur": err})
                continue
            for e in entrees:
                dt = date_entree(e)
                lien = e.get("link") or ""
                if dt is None or dt < limite or not lien:
                    continue
                articles.append({
                    "d": dt.strftime("%Y-%m-%d"),
                    "s": nom[:60],
                    "t": nettoyer(e.get("title"), 200),
                    "u": lien,
                    "x": nettoyer(e.get("summary") or e.get("description"), 400),
                })

    vus, uniques = set(), []
    for a in sorted(articles, key=lambda a: a["d"], reverse=True):
        if a["u"] not in vus:
            vus.add(a["u"])
            uniques.append(a)

    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.startswith("chunk-"):
            os.remove(os.path.join(OUT, f))

    chunks = []
    for i in range(0, len(uniques), TAILLE_CHUNK):
        nom_fichier = f"chunk-{i // TAILLE_CHUNK + 1:02d}.json"
        with open(os.path.join(OUT, nom_fichier), "w", encoding="utf-8") as fh:
            json.dump(uniques[i:i + TAILLE_CHUNK], fh, ensure_ascii=False)
        chunks.append(nom_fichier)

    index = {
        "genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fenetre_jours": FENETRE_JOURS,
        "nb_flux": len(flux),
        "nb_flux_en_erreur": len(erreurs),
        "nb_articles": len(uniques),
        "chunks": chunks,
        "erreurs": erreurs[:40],
    }
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    print(f"{len(uniques)} articles | {len(erreurs)} flux en erreur | {len(chunks)} chunks")


if __name__ == "__main__":
    main()
