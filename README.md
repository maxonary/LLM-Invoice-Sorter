# Why this script? - Gmail Invoice Sorting
I didn't want to download and sort all my emails manually for the yearly invoicing, so I wrote an LLM bot script to do it for me. 

Automatically fetch, categorize, rename and sort invoices from Gmail or local folders using a local LLM (via Ollama) or ChatGPT (via OpenAI API).

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
Rename files using the first detected date in the PDF (format: Year-Month-Day).  
If `--calendar-context` is provided, filenames will include a short event keyword from your calendar (e.g., `2024-06-13-meeting.pdf`).

You can also use `--rename-by-date` with `--no-gmail` or `--gmail-only` to rename files in the temp_invoices/ folder or the downloaded Gmail PDFs.

## Calendar Context (optional)

You can provide one or more `.ics` calendar files using the `--calendar-context` flag to enrich file names based on your schedule.

Example:
```bash
python main.py --rename-by-date --calendar-context calendar.ics
```

This allows the script to include contextual slugs in filenames, like `2024-06-13-kickoff.pdf` or `2024-07-01-vacation.pdf`, based on events scheduled that day.
