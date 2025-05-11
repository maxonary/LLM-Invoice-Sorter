# Why this script? - Gmail Invoice Sorting
I didn't want to download and sort all my emails manually for the yearly invoicing, so I wrote an LLM bot script to do it for me. 

Automatically fetch, categorize, and sort invoices from Gmail or local folders using a local LLM (via Ollama) or ChatGPT (via OpenAI API).

Make sure to download your Gmail OAuth 2.0 Credentials Json from your Google Cloud Console after enabling the Gmail API. 

## Installing 
Install dependencies into a venv with UV. Make sure to have UV installed.
```bash
uv sync
```
Then enable the venv.

## Running
`python main.py`  
Process Gmail + local PDFs (default)

`python main.py --no-gmail`  
Only categorize PDFs dropped in temp_invoices/

`python main.py --gmail-only`  
Only scan Gmail and attempt to download all invoice PDFs

`python main.py --rename-by-date`  
Rename files using the first detected date in the PDF and its category (e.g., 12-04-2024_Travel.pdf), Day Month Year. 

You can also use `--rename-by-date` with `--no-gmail` or `--gmail-only` to rename files in the temp_invoices/ folder or the downloaded Gmail PDFs.
