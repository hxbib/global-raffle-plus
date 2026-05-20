# Global Raffle+

A modular Python automation toolkit that orchestrates multi-stage workflows across HTTP APIs, browser engines, email protocols, and mobile devices -- built as eight independent CLI tools connected through a shared filesystem pipeline.

## Overview

Global Raffle+ automates account lifecycle and engagement on the Global Citizen platform (`globalcitizen.org` and its Android app). The toolkit spans the full pipeline: synthetic data generation, account registration via reverse-engineered API flows, email confirmation through IMAP parsing, session capture, bulk API interaction, GraphQL-based status polling, and Android UI automation via Appium.

Each module is a standalone CLI script. They communicate through CSV files, plain-text configs, per-account cookie dumps, SQLite databases, and NDJSON response logs -- no shared runtime, no central server.

## Why I Built This

I REALLY, and I mean REALLY wanted to see The Weeknd perform at the concert - especially since apparently, he is going to retire his "The Weeknd" showname. I ended up winning tickets. He ended up pulling out near last minute. Well deserved, I'd say.

## Features

- **Account Data Generation** -- Produces structured CSVs with realistic names (Faker), US ZIP codes (pgeocode), and randomized credentials
- **Request-Based Account Registration** -- Generate new accounts with information in CSV files via requests.
- **Email Confirmation (Selenium)** -- Visits confirmation links in headless Chrome via selenium-wire, captures HTTP status from browser network traffic, handles unsubscribe/resubscribe page flows
- **Email Confirmation (Async IMAP + Camoufox)** -- Connects to Gmail over IMAP-SSL, parses MIME-encoded HTML emails, scores anchor elements with a weighted heuristic to identify confirm buttons, visits links through anti-fingerprinting Firefox (Camoufox) with spoofed US geolocation (accounts started getting mass banned)
- **Session Cookie Capture** -- Authenticates via the platform's login API, captures per-account headers and cookies, persists them in a parseable text format for downstream modules (this isn't good practice, but it was quick, one time, use and I'll never need, use, or look at this again)
- **Bulk Action Completion** -- Replays captured sessions against the action-completion API for a list of action IDs, with per-account JSON resume state, 429-aware proxy rotation, and exponential backoff - if any "actions" are added, it is auto completed via requests.
- **GraphQL Reward Status Checker** -- Paginates the `GetEnteredRewards` GraphQL query per account, aggregates status tallies, and reports results with per-reward breakdowns
- **Android App Automation** -- Drives the Global Citizen Android app via Appium UIAutomator2: login, navigate, interact, logout, and wipe app data per account in sequence

## Tech Stack


| Category           | Technologies                                                                      |
| ------------------ | --------------------------------------------------------------------------------- |
| Language           | Python 3                                                                          |
| HTTP               | `requests`, `urllib3` (retry adapter), `HTTPAdapter`                              |
| Data               | `pandas`, `csv`, `sqlite3`, `json`                                                |
| Browser Automation | `selenium`, `selenium-wire`, `camoufox` (Playwright/Firefox), `webdriver-manager` |
| Mobile Automation  | `Appium-Python-Client`, UIAutomator2                                              |
| Email              | `imaplib`, `email` (stdlib), `beautifulsoup4` (HTML parsing)                      |
| Data Generation    | `faker`, `pgeocode`                                                               |
| Concurrency        | `concurrent.futures.ThreadPoolExecutor`, `asyncio`, `threading`                   |
| Config             | `python-dotenv`, `argparse`                                                       |


## Architecture

```
csvCreator ──> accounts.csv
                   │
                   v
               accGen ──> success.csv ──> (confirmation emails sent)
                                              │
                                              v
                         emailConfirm / imapEmailConfirm
                                              │
                                              v
                                        cookieCapture
                                              │
                                              v
                            accountCookies/{email}/{email}.txt
                                        │             │
                                        v             v
                                    pointFarm     winCheck
                                                      │
                                                      v
                                              (status report)

                                    androidBot (parallel path, uses
                                    accountInfo.csv directly)
```

## How It Works

1. `**csvCreator**` generates synthetic account data (names, emails, passwords, NY ZIP codes) into a CSV
2. `accGen` reads the CSV and registers each account.
3. `**emailConfirm**` or `imapEmailConfirm` confirms each account's email by visiting the confirmation link in a headless browser. The IMAP variant autonomously fetches links from Gmail.
4. `cookieCapture` logs into each confirmed account and persists session headers/cookies to disk.
5. `searchIDRequest` discovers available action IDs by paginating a recommendations API.
6. `pointFarm` replays each account's captured session against the action-completion endpoint for every discovered ID.
7. `winCheck` queries a GraphQL endpoint per account to check reward/raffle outcomes.
8. `androidBot` (alternative path) automates the mobile app directly via Appium.

## Installation / Setup

```bash
# Clone the repository
git clone https://github.com/<hxbib>/global-raffle-plus.git
cd global-raffle-plus-main

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt
```

### Module-specific setup

- **Proxies**: populate `<module>/proxies.txt` with proxy entries (one per line), or write `none` for direct connections
- **IMAP (imapEmailConfirm)**: copy `.env.example` to `imapEmailConfirm/.env` and fill in Gmail credentials (requires a Google App Password)
- **Android (androidBot)**: requires an Android device/emulator with the app installed, ADB available, and Appium server running at `http://127.0.0.1:4723`

## Usage

Each module is invoked independently:

```bash
python csvCreator/csvCreator.py
python accGen/reqAccGen.py --workers 50 [--no-proxy]
python emailConfirm/emailConfirm.py
python imapEmailConfirm/emailConfirm.py
python cookieCapture/reqCookieCapture.py --workers 50 [--verbose] [--force]
python pointFarm/searchIDRequest.py
python pointFarm/pointFarm.py [--workers 100] [--max-retries 8]
python winCheck/winCheck.py [--workers 4] [--status ALL]
python androidBot/androidBot.py
```

