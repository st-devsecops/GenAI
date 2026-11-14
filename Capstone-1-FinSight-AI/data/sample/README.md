# Sample corpus

All four documents in this folder are **synthetic**: the companies (Trivenza Bank, Solvantra Finance) and the regulator (FSRA) are fictional, created so the project runs out-of-the-box and the evaluation set has verifiable answers.

To work with real, free public documents, create the folder (`mkdir data/raw`), download them into it and run `python -m scripts.ingest data/raw` (or add `--api http://localhost:8000` while the API is running). `data/raw/` is git-ignored on purpose - do not commit third-party PDFs:

| Source | Where to download (free) |
| --- | --- |
| RBI circulars & master directions | rbi.org.in -> Notifications / Master Directions |
| SEBI circulars | sebi.gov.in -> Legal -> Circulars |
| Listed company annual reports | NSE/BSE company pages or the company's Investor Relations page |
| Quarterly results / investor presentations | Company Investor Relations page |

Scanned PDFs must be OCR'd first with the free tool `ocrmypdf` (`brew install ocrmypdf` / `apt install ocrmypdf`).
