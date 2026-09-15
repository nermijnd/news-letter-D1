"""
Newsletter hebdomadaire automatique
------------------------------------
Ce script :
1. Récupère des brèves d'actualité (BBC, Le Monde, The Guardian, Reuters)
2. Récupère des indicateurs macro-économiques fiables via l'API FRED
3. Construit un email en HTML
4. L'envoie par Gmail à la liste de destinataires (recipients.txt)
5. Sauvegarde une copie dans docs/ pour la page web partageable (GitHub Pages)
"""

import os
import re
import ssl
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import feedparser
import requests

# --- Configuration -----------------------------------------------------

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
FRED_API_KEY = os.environ.get("FRED_API_KEY")

NEWS_FEEDS = {
    "BBC News (Monde)": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Le Monde (Une)": "https://www.lemonde.fr/rss/une.xml",
    "The Guardian (Monde)": "https://www.theguardian.com/world/rss",
    "Reuters (Monde)": "https://www.reutersagency.com/feed/?best-topics=world&post_type=best",
}

# Séries de données officielles (FRED agrège Eurostat, OCDE, FMI, Banque mondiale…)
# Ce sont des sources primaires, considérées parmi les plus fiables au monde.
# Valeurs directes (taux, indices) : on affiche simplement la dernière donnée publiée.
FRED_SERIES = {
    "Inflation US (indice des prix, CPI)": "CPIAUCSL",
    "Taux de chômage US": "UNRATE",
    "Taux directeur de la Fed (Fed Funds Rate)": "FEDFUNDS",
    "Taux des bons du Trésor US à 10 ans": "DGS10",
    "Inflation France (CPI, variation annuelle %)": "CPALTT01FRM657N",
    "Taux de chômage France": "LRHUTTTTFRM156S",
    "Taux de chômage Zone Euro": "LRHUTTTTEZM156S",
    # Donnée officielle chinoise du chômage limitée (zones urbaines uniquement) ;
    # à défaut d'API fiable pour ce chiffre, on utilise le chômage des jeunes (Banque mondiale).
    "Taux de chômage des jeunes en Chine (15-24 ans)": "SLUEM1524ZSCHN",
    "Taux d'intérêt long terme France (10 ans)": "IRLTLT01FRM156N",
}

# Séries de niveau du PIB réel : on calcule nous-mêmes la variation en % par rapport
# à la période précédente, ce qui est plus fiable que de dépendre de séries de
# "croissance" toutes faites (certaines ne sont plus mises à jour).
GDP_SERIES = {
    "Croissance du PIB France (trimestrielle)": "CLVMNACSCAB1GQFR",
    "Croissance du PIB Zone Euro (trimestrielle)": "CLVMNACSCAB1GQEA19",
    "Croissance du PIB Chine (annuelle)": "NGDPRXDCCNA",
}

MAX_ITEMS_PER_FEED = 4
SUMMARY_MAX_LEN = 280


# --- Récupération des news ----------------------------------------------

def strip_html(text):
    """Retire les balises HTML basiques d'un texte."""
    return re.sub(r"<[^<]+?>", "", text or "").strip()


def fetch_news():
    results = {}
    for name, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
                title = strip_html(entry.get("title", ""))
                summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
                if len(summary) > SUMMARY_MAX_LEN:
                    summary = summary[:SUMMARY_MAX_LEN].rsplit(" ", 1)[0] + "…"
                link = entry.get("link", "")
                if title:
                    items.append({"title": title, "summary": summary, "link": link})
            results[name] = items
        except Exception as exc:  # on ne bloque jamais toute la newsletter pour une source en panne
            results[name] = [{"title": "Source indisponible pour le moment", "summary": str(exc), "link": ""}]
    return results


# --- Récupération des données macro (FRED) -------------------------------

def fetch_fred_series(series_id):
    if not FRED_API_KEY:
        return None, "Clé FRED_API_KEY manquante"
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json()["observations"][0]
        return obs["date"], obs["value"]
    except Exception as exc:
        return None, f"Erreur ({exc})"


def fetch_macro():
    results = {}
    for label, series_id in FRED_SERIES.items():
        date, value = fetch_fred_series(series_id)
        results[label] = {"date": date, "value": value}
    return results


def fetch_fred_growth(series_id):
    """Récupère les 2 dernières valeurs d'une série de niveau (ex: PIB) et calcule
    la variation en % par rapport à la période précédente."""
    if not FRED_API_KEY:
        return None, "Clé FRED_API_KEY manquante"
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 2,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json()["observations"]
        latest, previous = obs[0], obs[1]
        latest_val = float(latest["value"])
        previous_val = float(previous["value"])
        growth_pct = (latest_val - previous_val) / previous_val * 100
        return latest["date"], f"{growth_pct:+.2f} %"
    except Exception as exc:
        return None, f"Erreur ({exc})"


def fetch_gdp_growth():
    results = {}
    for label, series_id in GDP_SERIES.items():
        date, value = fetch_fred_growth(series_id)
        results[label] = {"date": date, "value": value}
    return results


# --- Construction du HTML -------------------------------------------------

def build_html(news, macro, today_str):
    macro_rows = ""
    for label, data in macro.items():
        date = data["date"] or "N/A"
        macro_rows += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee;">{label}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;font-weight:bold;">{data['value']}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;color:#888;font-size:12px;">{date}</td>
        </tr>"""

    news_sections = ""
    for source, items in news.items():
        articles_html = ""
        for item in items:
            link_html = f'<a href="{item["link"]}" style="color:#1a73e8;">{item["title"]}</a>' if item["link"] else item["title"]
            articles_html += f"""
            <div style="margin-bottom:14px;">
                <div style="font-weight:600;">{link_html}</div>
                <div style="color:#555;font-size:14px;">{item['summary']}</div>
            </div>"""
        news_sections += f"""
        <h3 style="margin-top:28px;border-bottom:2px solid #222;padding-bottom:4px;">{source}</h3>
        {articles_html}"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto;color:#222;">
    <h1 style="border-bottom:4px solid #222;padding-bottom:8px;">📬 Newsletter de la semaine</h1>
    <p style="color:#888;">Édition du {today_str}</p>

    <h2 style="margin-top:30px;">📊 Indicateurs macro-économiques</h2>
    <p style="color:#666;font-size:13px;">Source : FRED (Réserve fédérale américaine) — donnée la plus récente disponible pour chaque indicateur.</p>
    <table style="width:100%;border-collapse:collapse;">{macro_rows}</table>

    <h2 style="margin-top:30px;">🌍 Revue de presse internationale</h2>
    {news_sections}

    <p style="margin-top:40px;color:#999;font-size:12px;">
        Généré automatiquement chaque semaine. Les résumés sont de courts extraits ;
        cliquez sur les titres pour lire l'article complet sur le site de la source.
    </p>
</body>
</html>"""
    return html


# --- Envoi de l'email -------------------------------------------------

def send_email(html_content, subject, recipients):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())


# --- Archive / page web -------------------------------------------------

def save_to_archive(html_content, today_str):
    os.makedirs("docs/archive", exist_ok=True)
    filename = f"docs/archive/{today_str}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)
    update_index()


def update_index():
    archive_dir = "docs/archive"
    files = sorted(
        (f for f in os.listdir(archive_dir) if f.endswith(".html")),
        reverse=True,
    )
    items = "\n".join(
        f'<li style="margin-bottom:8px;"><a href="archive/{f}" style="font-size:16px;">{f.replace(".html", "")}</a></li>'
        for f in files
    )
    index_html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>Ma Newsletter — Archives</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:40px auto;color:#222;">
    <h1>📬 Ma Newsletter — Archives</h1>
    <p style="color:#666;">Retrouve ici tous les anciens numéros.</p>
    <ul style="list-style:none;padding:0;">
        {items}
    </ul>
</body>
</html>"""
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(index_html)


# --- Programme principal -------------------------------------------------

def main():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("Récupération des actualités…")
    news = fetch_news()

    print("Récupération des données macro…")
    macro = fetch_macro()
    macro.update(fetch_gdp_growth())

    print("Construction de l'email…")
    html_content = build_html(news, macro, today_str)

    print("Sauvegarde dans l'archive / page web…")
    save_to_archive(html_content, today_str)

    recipients = []
    if os.path.exists("recipients.txt"):
        with open("recipients.txt", "r", encoding="utf-8") as f:
            recipients = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

    if recipients and GMAIL_USER and GMAIL_APP_PASSWORD:
        print(f"Envoi de l'email à {len(recipients)} destinataire(s)…")
        send_email(html_content, f"Newsletter hebdo — {today_str}", recipients)
        print("Email envoyé !")
    else:
        print(
            "Email NON envoyé : vérifie que GMAIL_USER, GMAIL_APP_PASSWORD "
            "sont bien configurés (secrets GitHub) et que recipients.txt "
            "contient au moins une adresse."
        )


if __name__ == "__main__":
    main()
