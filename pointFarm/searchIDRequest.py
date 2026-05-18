import requests
import json
from urllib.parse import urljoin
from pathlib import Path

BASE_URL = "https://api.globalcitizen.org"
ENDPOINT = "/v1/me/recommendations/actions/"

params = {
    "offset": 0,
    "limit": 500,
}

headers = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9,bn;q=0.8",
    "apikey": "7292e0560c6b44258c66fd60f0a19a32",
    "dnt": "1",
    "origin": "https://www.globalcitizen.org",
    "priority": "u=1, i",
    "referer": "https://www.globalcitizen.org/",
    "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "x-requested-with": "XMLHttpRequest"
}


def fetch_all():
    url = urljoin(BASE_URL, ENDPOINT)
    all_results = []
    seen_nexts = set()

    r = requests.get(url, headers=headers, params=params, timeout=30)
    print(r.status_code)
    data = r.json()

    all_results.extend(data.get("results", []))
    next_url = data.get("next")

    while next_url and next_url not in seen_nexts:
        seen_nexts.add(next_url)
        page_url = urljoin(BASE_URL, next_url)
        r = requests.get(page_url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"Stopped on non-200 ({r.status_code}) for {page_url}")
            break
        data = r.json()
        all_results.extend(data.get("results", []))
        next_url = data.get("next")

    return all_results

if __name__ == "__main__":
    try:
        results = fetch_all()
        print(f"Fetched {len(results)} items.")

        ids = [str(item.get("id")) for item in results if "id" in item]

        output_path = Path(__file__).parent / "ids.txt"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(ids))

        print(f"Saved {len(ids)} IDs to {output_path}")

    except Exception as e:
        print("Request failed:", repr(e))
