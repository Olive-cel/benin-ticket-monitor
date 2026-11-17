
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

URL = "https://voyage.benin.bj/"

def check_website():
    print("Verification de la billetterie du Benin...")

    response = requests.get(URL, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    if "sera bientôt disponible" in text:
        print("Billetterie pas encore ouverte.")
    else:
        print("ATTENTION : message d'attente absent.")
        print("Verifier manuellement :", URL)

    print("Verification terminee :", datetime.now(timezone.utc))

if __name__ == "__main__":
    check_website()
