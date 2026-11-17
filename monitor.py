
import os
import re
import sys
import smtplib
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

URL = "https://voyage.benin.bj/"
ISSUE_TITLE = "[monitor-state] benin-ticket-monitor"
API = "https://api.github.com"


def normalize(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(
        c for c in text if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", " ", text).strip()


def send_email(subject, body):
    required = ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL")
    missing = [key for key in required if not os.getenv(key)]

    if missing:
        raise RuntimeError(
            "Secrets manquants: " + ", ".join(missing)
        )

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

    print("Email envoye avec succes.")


def github(method, path, **kwargs):
    token = os.environ["GH_TOKEN"]

    response = requests.request(
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

    issues = github(
        "GET",
        f"/repos/{repo}/issues",
        params={"state": "all", "per_page": 100}
    )

    matches = [
        issue for issue in issues
        if issue.get("title") == ISSUE_TITLE
        and "pull_request" not in issue
    ]

    if len(matches) > 1:
        raise RuntimeError(
            "Plusieurs issues de suivi trouvees"
        )

    if not matches:
        return None, None

    issue = matches[0]
    return issue["number"], issue.get("body", "").strip()


def save_state(number, status):
    repo = os.environ["GITHUB_REPOSITORY"]

    payload = {
        "title": ISSUE_TITLE,
        "body": status,
    }

    if number is None:
        github(
            "POST",
            f"/repos/{repo}/issues",
            json=payload
        )
    else:
        github(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            json=payload
        )


def inspect_site():
    response = requests.get(
        URL,
        timeout=25,
        headers={
            "User-Agent": "Mozilla/5.0 BeninTicketMonitor/1.0"
        }
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = normalize(
        soup.get_text(" ", strip=True)
    )

    if "sera bientot disponible" in text:
        return "waiting"

    if len(text) < 50:
        raise RuntimeError(
            "Page vide ou contenu insuffisant"
        )

    # Ne pas assimiler une disparition du message
    # a une ouverture officielle des reservations.
    return "unconfirmed"


def main():
    if "--test-email" in sys.argv:
        send_email(
            "TEST - Surveillance billets Benin",
            "Ceci est un email de test.\n\n"
            "Ton robot GitHub Actions peut envoyer "
            "des emails.\n\n"
            + URL
        )
        return

    status = inspect_site()

    now = datetime.now(timezone.utc).isoformat()
    print(f"{now} - Statut: {status}")

    number, previous = get_state()

    if previous == status:
        print("Aucun changement.")
        return

    # La disponibilite n'est pas encore confirmee
    # par ce detecteur HTML.
    # Aucune alerte de billets n'est donc envoyee.
    save_state(number, status)

    print("Etat memorise :", status)

    if status == "unconfirmed":
        print(
            "Changement detecte, mais ouverture "
            "non confirmee. Aucune alerte envoyee."
        )


if __name__ == "__main__":
    main()
