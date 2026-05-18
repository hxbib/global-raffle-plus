
import csv
import os
import random
import string
import sys
from datetime import datetime
from typing import List

def ensure_package(mod_name: str, pip_name: str = None):
    try:
        __import__(mod_name)
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name or mod_name])
        __import__(mod_name)

ensure_package("faker")
ensure_package("pgeocode")

from faker import Faker
import pgeocode

def rand_letters(n_min=4, n_max=7) -> str:
    n = random.randint(n_min, n_max)
    return ''.join(random.choices(string.ascii_lowercase, k=n))

def rand_digits(n_min=2, n_max=4) -> str:
    n = random.randint(n_min, n_max)
    return ''.join(random.choices(string.digits, k=n))

def rand_password(length=7) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choices(alphabet, k=length))

def get_ny_zipcodes() -> List[str]:
    nomi = pgeocode.Nominatim('us')
    df = nomi._data
    ny = df[(df['state_name'] == 'New York') & df['postal_code'].str.fullmatch(r'\d{5}', na=False)]
    zips = ny['postal_code'].dropna().astype(str).unique().tolist()
    zips.sort()
    return zips

def main():
    random.seed()
    fake = Faker("en_US")

    ny_zips = get_ny_zipcodes()
    if not ny_zips:
        raise RuntimeError("Could not find New York ZIP codes. Check your internet/installation and try again.")

    timestamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    filename = f"csvCreated {timestamp}.csv"
    out_path = os.path.join(os.getcwd(), filename)

    headers = ["Email", "Password", "First Name", "Last Name", "Username", "Zipcode"]

    rows_needed = 5000
    rows = []

    zip_index = 0
    for i in range(rows_needed):
        prefix = rand_letters(4, 7)
        suffix = rand_digits(2, 4)
        local_part = prefix + suffix
        email = f"{local_part}@domain.com"
        username = local_part
        password = rand_password(7)
        first = fake.first_name()
        last = fake.last_name()
        zipcode = ny_zips[zip_index]

        rows.append([email, password, first, last, username, zipcode])

        zip_index += 1
        if zip_index >= len(ny_zips):
            zip_index = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"Done! Wrote {rows_needed} rows to '{filename}' in:\n{os.getcwd()}")

if __name__ == "__main__":
    main()
