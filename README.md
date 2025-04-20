# Why this script? - Gmail Invoice Sorting
I didn't want to download and sort all my emails manually for the yearly invoicing, so I wrote an LLM bot script to do it for me. 

Automatically fetch, categorize, and sort invoices from Gmail or local folders using a local LLM (via Ollama) or ChatGPT (via OpenAI API).

Make sure to download your Gmail OAuth 2.0 Credentials Json from your Google Cloud Console after enabling the Gmail API. 

`python main.py` Process Gmail + local PDFs (default)
`python main.py --no-gmail` Only categorize PDFs dropped in temp_invoices/
`python main.py --gmail-only` Only scan Gmail and attempt to download all invoice PDFs
