
import hashlib
import json
import os
import re
import smtplib
import sys
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

URL = "https://voyage.benin.bj/"
API = "https://api.github.com"
ISSUE_TITLE = "[monitor-state] benin-ticket-monitor"

BOOKING_TERMS = (
    "reserver", "reservation", "billetterie",
    "acheter un billet", "acheter votre billet",
    "book now", "book flight", "buy ticket"
)

AVAILABLE_TERMS = (
    "places disponibles", "billets disponibles",
    "vols disponibles", "vol disponible",
    "selectionner un vol", "choisir un vol"
)

NEGATIVE_TERMS = (
    "bientot disponible", "prochainement",
    "aucun vol disponible", "aucun billet disponible",
    "aucune place disponible", "complet",
    "sold out", "coming soon"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 BeninTicketMonitor/3.0"
})


def normalize(value):
    text = unicodedata.normalize("NFKD", str(value).lower())
    text = "".join(
        c for c in text if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", " ", text).strip()


def send_email(subject, body):
    required = ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL")
    missing = [k for k in required if not os.getenv(k)]

    if missing:
        raise RuntimeError("Secrets manquants: " + ", ".join(missing))

    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_EMAIL"]
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, timeout=25
    ) as smtp:
        smtp.login(
            os.environ["SMTP_USER"],
            os.environ["SMTP_PASSWORD"].replace(" ", "")
        )
        smtp.send_message(msg)

    print("Email envoye.")


def github(method, path, **kwargs):
    token = os.environ["GH_TOKEN"]

    response = session.request(
        method,
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
        **kwargs
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def get_state():
    repo = os.environ["GITHUB_REPOSITORY"]

    for page in range(1, 11):
        issues = github(
            "GET",
            f"/repos/{repo}/issues",
            params={
                "state": "all",
                "per_page": 100,
                "page": page
            }
        )

        matches = [
            item for item in issues
            if item.get("title") == ISSUE_TITLE
            and "pull_request" not in item
        ]

        if len(matches) > 1:
            raise RuntimeError("Plusieurs issues de suivi")

        if matches:
            item = matches[0]
            raw = (item.get("body") or "").strip()

            try:
                state = json.loads(raw)
                if not isinstance(state, dict):
                    raise ValueError()
            except ValueError:
                if raw not in ("waiting", "unconfirmed"):
                    raise RuntimeError("Etat GitHub inconnu")
                state = {}

            state.setdefault("initialized", False)
            state.setdefault("change_signature", None)
            state.setdefault("availability_signature", None)

            return item["number"], state

        if len(issues) < 100:
            break

    return None, {
        "initialized": False,
        "change_signature": None,
        "availability_signature": None
    }


def save_state(number, state):
    repo = os.environ["GITHUB_REPOSITORY"]
    payload = {
        "title": ISSUE_TITLE,
        "body": json.dumps(state, ensure_ascii=False, indent=2)
    }

    if number is None:
        result = github(
            "POST", f"/repos/{repo}/issues", json=payload
        )
        return result["number"]

    github(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        json=payload
    )
    return number


def fetch_page(url):
    response = session.get(url, timeout=20)
    response.raise_for_status()

    if not same_site(response.url):
        raise RuntimeError("Redirection hors du site officiel")

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = normalize(soup.get_text(" ", strip=True))

    if len(text) < 50:
        raise RuntimeError("Contenu insuffisant")

    return soup, text


def same_site(url):
    return (
        urlparse(url).scheme == "https"
        and urlparse(url).hostname == urlparse(URL).hostname
    )


def find_booking_links(soup, base_url):
    links = []

    for a in soup.find_all("a", href=True):
        label = normalize(a.get_text(" ", strip=True))
        href = urljoin(base_url, a["href"])
        path = normalize(urlparse(href).path)

        if not same_site(href):
            continue

        if any(
            word in label or word in path
            for word in BOOKING_TERMS
        ):
            links.append(href)

    return sorted(set(links))


def booking_form(soup):
    for form in soup.find_all("form"):
        fields = " ".join(
            normalize(
                str(tag.get("name", "")) + " "
                + str(tag.get("id", ""))
            )
            for tag in form.find_all(["input", "select"])
        )

        has_trip_fields = any(
            word in fields
            for word in (
                "flight", "departure", "destination",
                "depart", "date", "origine"
            )
        )

        has_action = any(
            word in normalize(form.get_text(" ", strip=True))
            for word in BOOKING_TERMS
        )

        if has_trip_fields and has_action:
            return True

    return False


def inspect_site():
    soup, text = fetch_page(URL)

    waiting = "sera bientot disponible" in text

    links = find_booking_links(soup, URL)
    form_present = booking_form(soup)

    # Signature du contenu visible.
    # Ignore les espaces et les accents.
    signature = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

    available_link = None

    if not waiting:
        for link in links[:5]:
            try:
                sub_soup, sub_text = fetch_page(link)
            except (requests.RequestException, RuntimeError) as exc:
                print("Verification impossible:", exc)
                continue

            positive = any(
                term in sub_text for term in AVAILABLE_TERMS
            )
            negative = any(
                term in sub_text for term in NEGATIVE_TERMS
            )

            if positive and not negative and booking_form(sub_soup):
                # Indices forts mais pas encore preuve
                # d'une transaction effectivement possible.
                available_link = link
                break

    return {
        "waiting": waiting,
        "signature": signature,
        "links": links,
        "form_present": form_present,
        "available_link": available_link,
        "text": text
    }


def main():
    if "--test-email" in sys.argv:
        send_email(
            "TEST - Surveillance billets Benin",
            "Le robot peut envoyer des emails.\n\n" + URL
        )
        return

    result = inspect_site()
    number, state = get_state()
    now = datetime.now(timezone.utc).isoformat()

    signature = result["signature"]

    if not state.get("initialized"):
        state["initialized"] = True
        state["change_signature"] = signature
        state["waiting"] = result["waiting"]
        state["last_check"] = now

        save_state(number, state)
        print("Etat initial memorise. Pas d'alerte.")
        return

    previous_signature = state.get("change_signature")
    previous_waiting = state.get("waiting", True)

    changed = (
        previous_signature != signature
        or previous_waiting != result["waiting"]
    )

    availability = result["available_link"]

    if changed:
        if previous_waiting and not result["waiting"]:
            reason = "Le message 'bientot disponible' a disparu."
        else:
            reason = "Le contenu visible de la page a change."

        subject = "CHANGEMENT - Site billets Benin"

        body = (
            "Un changement a ete detecte sur le site.\n\n"
            f"Motif : {reason}\n\n"
            f"Page officielle : {URL}\n\n"
            "Verifie rapidement si les reservations sont ouvertes."
        )

        # On n'envoie pas d'alerte de changement
        # pour les modifications mineures de mise en forme.
        send_email(subject, body)

        state["change_signature"] = signature
        state["waiting"] = result["waiting"]
        state["last_change"] = now
        number = save_state(number, state)

    if availability:
        availability_signature = hashlib.sha256(
            availability.encode("utf-8")
        ).hexdigest()

        if state.get("availability_signature") != availability_signature:
            send_email(
                "BILLETS BENIN - RESERVATION A VERIFIER",
                "Des indices forts de reservation ont ete detectes.\n\n"
                "La disponibilite reelle doit encore etre verifiee.\n\n"
                f"Lien : {availability}\n\n"
                f"Site officiel : {URL}"
            )

            state["availability_signature"] = availability_signature
            number = save_state(number, state)

    state["last_check"] = now
    save_state(number, state)

    print("Surveillance terminee.")
    print("Changement:", changed)
    print("Message d'attente:", result["waiting"])
    print("Lien de reservation candidat:", availability)


if __name__ == "__main__":
    main()
