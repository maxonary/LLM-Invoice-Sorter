import os
import time
import pickle
import base64
import shutil
import fitz
import ollama
import re
from bs4 import BeautifulSoup
import requests
import openai
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

def load_reviewed_ids(file_path="review_queue.csv"):
    if not os.path.exists(file_path):
        return set()
    with open(file_path, newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        next(reader, None)  # skip header
        return {row[3].split('/')[-1] for row in reader if len(row) > 3 and "mail.google.com" in row[3]}

load_dotenv()

# -------------- Config --------------
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
DOWNLOAD_DIR = 'temp_invoices'
SORTED_DIR = 'Invoices'
MODEL = 'mistral'  # or 'llama2'
USE_OPENAI_KEY = os.getenv("USE_OPENAI", False)  # Set to True to use ChatGPT instead of Ollama
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") # Set this in your .env file
if not OPENAI_API_KEY and USE_OPENAI_KEY:
    raise ValueError("OPENAI_API_KEY must be set if USE_OPENAI_KEY is True")

OPENAI_MODEL = "gpt-3.5-turbo"

KEYWORDS = ["RECHNUNG", "INVOICE", "BELEG"]
START_DATE = "2023/01/01"  # format: YYYY/MM/DD or None to use TIMEFRAME
TIMEFRAME = "1y" # options: 1d, 7d, 30d, 1y etc.
CATEGORIES = [
    "Work Equipment",       # Tools, office supplies, hardware for work
    "Insurance",            # Health, liability, or travel insurance
    "Travel",      # Train tickets, flights, taxis, parking, hotel, carsharing
    "Food",                 # Meals, restaurants
    "Lifestyle",            # Non-deductible: entertainment, subscriptions, etc.
    "Other"                 # Uncategorized or unclear
]
BLACKLISTED_SENDERS = [
    "noreply@paypal.com",
    "service@paypal.de",
    "no-reply@payments.google.com",
    "noreply@accounts.google.com",
    "notification@facebookmail.com",
    "noreply@apple.com"
]

if USE_OPENAI_KEY:
    openai.api_key = OPENAI_API_KEY

def write_to_review_queue(subject, url, reason, message_id=None):
    file_path = "review_queue.csv"
    entry = (subject.strip(), url.strip())

    existing_entries = set()
    if os.path.exists(file_path):
        with open(file_path, newline="", encoding="utf-8") as file:
            reader = csv.reader(file)
            next(reader, None)
            for row in reader:
                if len(row) >= 2:
                    existing_entries.add((row[0].strip(), row[1].strip()))

    if entry in existing_entries:
        return  # Avoid duplicates

    file_exists = os.path.exists(file_path)
    with open(file_path, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Subject", "URL", "Reason", "Gmail Link"])
        link = f"https://mail.google.com/mail/u/0/#inbox/{message_id}" if message_id else "N/A"
        writer.writerow([subject, url, reason, link])

def build_search_query(keywords, timeframe, start_date=None):
    keyword_part = " OR ".join(keywords)
    if start_date:
        return f"({keyword_part}) after:{start_date} before:2024/01/01"
    return f"({keyword_part}) newer_than:{timeframe}"

# -------------- Gmail Auth --------------
def gmail_authenticate():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)

# -------------- Gmail Message Search --------------
def search_messages(service, query):
    all_messages = []
    next_page_token = None

    while True:
        response = service.users().messages().list(
            userId='me',
            q=query,
            pageToken=next_page_token
        ).execute()

        all_messages.extend(response.get('messages', []))
        next_page_token = response.get('nextPageToken')

        if not next_page_token:
            break

    return all_messages

# -------------- Download PDF Attachments --------------
def download_attachments(service, message_id, save_dir):
    message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    found = False
    for part in message['payload'].get('parts', []):
        if part['filename'].lower().endswith('.pdf') and 'attachmentId' in part['body']:
            found = True
            attachment_id = part['body']['attachmentId']
            attachment = service.users().messages().attachments().get(
                userId='me', messageId=message_id, id=attachment_id).execute()
            data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
            filename = part['filename']
            filepath = os.path.join(save_dir, filename)
            base, ext = os.path.splitext(filepath)
            counter = 1
            while os.path.exists(filepath):
                filepath = f"{base}_{counter}{ext}"
                counter += 1
            with open(filepath, 'wb') as f:
                f.write(data)
            print(f"[✓] Downloaded: {filepath}")
            yield filepath
    if not found:
        subject = "No Subject"
        for header in message['payload'].get('headers', []):
            if header['name'] == 'Subject':
                subject = header['value']
                break
        write_to_review_queue(subject, "(no attachment)", "No PDF attachments", message_id)
# -------------- Extract Text from PDF --------------
def extract_text_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    return text[:2000]  # limit for faster LLM

# -------------- Extract Invoice Links with Ollama --------------
def extract_invoice_links_with_ollama(service, message_id):
    message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    parts = message['payload'].get('parts', [])
    body = ''
    for part in parts:
        if part.get('mimeType') == 'text/plain' and 'data' in part.get('body', {}):
            body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
        elif part.get('mimeType') == 'text/html' and 'data' in part.get('body', {}):
            body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')

    soup = BeautifulSoup(body, 'html.parser')
    candidates = []
    for a in soup.find_all('a'):
        href = a.get('href', '')
        label = a.text.strip()
        if href:
            candidates.append(f"{label} → {href}")
    joined_links = "\n".join(candidates)

    prompt = f"""
From the following list of link texts and their URLs, identify those that likely point to invoices, receipts, ticket downloads, or payment confirmations.

Only return the raw URLs. Prioritize PDF or download links labeled with words like "Beleg", "Rechnung", "Download", "PDF".

Links:
{joined_links}

PDF Links:
"""
    response = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    text = response['message']['content']
    raw_urls = re.findall(r'https?://\S+', text)
    urls = []
    for url in raw_urls:
        cleaned = url.strip(">)].,;\"'")
        if cleaned not in urls:
            urls.append(cleaned)
    if not urls:
        full_subject = "No Subject"
        for header in message['payload'].get('headers', []):
            if header['name'] == 'Subject':
                full_subject = header['value']
                break
        write_to_review_queue(full_subject, "(no link)", "No links extracted", message_id)
    return urls

# -------------- Download PDF from URL --------------
def download_pdf_from_url(url, save_dir, subject=None, message_id=None):
    try:
        response = requests.get(url, timeout=2)
        content_type = response.headers.get('content-type', '')
        if not response.content.strip():
            print(f"[!] Skipped empty file from {url}")
            if subject:
                write_to_review_queue(subject, url, "Empty response content", message_id)
            return None
        if content_type.startswith('application/pdf'):
            filename = os.path.basename(url.split("?")[0])
            filepath = os.path.join(save_dir, filename)
            base, ext = os.path.splitext(filepath)
            counter = 1
            while os.path.exists(filepath):
                filepath = f"{base}_{counter}{ext}"
                counter += 1
            with open(filepath, 'wb') as f:
                f.write(response.content)
            print(f"[✓] Downloaded from link: {filepath}")
            return filepath
        else:
            print(f"[!] Unexpected content-type from {url}: {content_type}")
            if subject:
                write_to_review_queue(subject, url, f"Unexpected content-type: {content_type}", message_id)
    except Exception as e:
        print(f"[!] Failed to download {url}: {e}")
        if subject:
            write_to_review_queue(subject, url, str(e), message_id)
    try:
        import browser_cookie3
        print(f"[i] Retrying with browser session cookies for {url}")
        cj = browser_cookie3.load()
        response = requests.get(url, cookies=cj, timeout=4)
        content_type = response.headers.get('content-type', '')
        if response.content.strip() and content_type.startswith('application/pdf'):
            filename = os.path.basename(url.split("?")[0])
            filepath = os.path.join(save_dir, filename)
            base, ext = os.path.splitext(filepath)
            counter = 1
            while os.path.exists(filepath):
                filepath = f"{base}_{counter}{ext}"
                counter += 1
            with open(filepath, 'wb') as f:
                f.write(response.content)
            print(f"[✓] Downloaded using session cookies: {filepath}")
            return filepath
        else:
            print(f"[!] Still not a valid PDF. Content-Type: {content_type}")
            if subject:
                write_to_review_queue(subject, url, f"Unexpected content-type (cookies): {content_type}", message_id)
    except Exception as e2:
        print(f"[!] Retry with browser cookies failed: {e2}")
        if subject:
            write_to_review_queue(subject, url, f"Cookie retry failed: {e2}", message_id)
    return None

# -------------- Categorize Invoice --------------
def categorize_invoice(text, model=MODEL):
    prompt = f"""
You are an invoice assistant. Categorize this invoice into one of the following categories:

- Work Equipment: Software Tools, office supplies, hardware purchased for work
- Insurance: Health, liability, or travel insurance
- Travel: Train tickets, flights, taxis, parking, hotel, carsharing, chauffeur, etc.
- Food: Meals, restaurant receipts (Keywords like "food", "restaurant", "meal", "catering", "bbq", "delivery", "bowl", "chicken", "pizza", "sushi, "burger", "snack", "drink", "beverage", "cafe", "breakfast", "lunch", "dinner")
- Lifestyle: Non-deductible items such as entertainment, personal subscriptions, hobbies, etc. (Keywords like "lifestyle", "subscription", "entertainment", "hobby", "personal", "gift", "clothing", "fashion", "accessory", "jewelry")
- Other: Anything that does not clearly belong to the above

Only respond with one category name.

Invoice:
{text}

Category:
"""
    if USE_OPENAI_KEY:
        response = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10
        )
        return response.choices[0].message['content'].strip()
    else:
        response = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}])
        return response['message']['content'].strip()

# -------------- Sort File to Category Folder --------------
def sort_file_to_category(file_path, category, text=None, rename_by_date=False, base_dir=SORTED_DIR):
    category = category if category in CATEGORIES else "Other"
    dest_dir = os.path.join(base_dir, category)
    os.makedirs(dest_dir, exist_ok=True)

    filename = os.path.basename(file_path)

    if rename_by_date and text:
        match = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', text)
        if match:
            day, month, year = match.groups()
            if len(year) == 2:
                year = '20' + year
            new_name = f"{year}-{int(month):02d}-{int(day):02d}.pdf"
            filename = new_name

    new_path = os.path.join(dest_dir, filename)
    base, ext = os.path.splitext(new_path)
    counter = 1
    while os.path.exists(new_path):
        new_path = f"{base}_{counter}{ext}"
        counter += 1

    shutil.move(file_path, new_path)
    print(f"[→] Sorted into: {category} as {os.path.basename(new_path)}")

# -------------- Process Dropped Invoices --------------
class InvoiceHandler(FileSystemEventHandler):
    def __init__(self, rename_by_date=False):
        super().__init__()
        self.rename_by_date = rename_by_date

    def on_any_event(self, event):
        if event.event_type not in ('created', 'moved'):
            return

        paths = []
        if event.is_directory:
            for root, _, files in os.walk(event.src_path):
                for file in files:
                    if file.lower().endswith('.pdf'):
                        paths.append(os.path.join(root, file))
        elif event.src_path.lower().endswith('.pdf'):
            paths.append(event.src_path)

        for file_path in paths:
            if not os.path.exists(file_path):
                print(f"[!] File does not exist yet: {file_path} (event: {event.event_type})")
                continue
            fname = os.path.basename(file_path)
            print(f"[i] Detected file event ({event.event_type}): {fname}")
            try:
                text = extract_text_from_pdf(file_path)
                print(f"[i] Categorizing manual file: {fname}")
                print(f"[i] Extracted text preview: {text[:100]}...")
                category = categorize_invoice(text)
                sort_file_to_category(file_path, category, text, self.rename_by_date)
            except Exception as e:
                print(f"[!] Error processing {fname}: {e}")

def process_dropped_invoices(rename_by_date=False):
    print(f"\n[i] Checking existing files in {DOWNLOAD_DIR} before watching for changes...")
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for file in files:
            if file.lower().endswith('.pdf'):
                file_path = os.path.join(root, file)
                if not os.path.exists(file_path):
                    continue
                fname = os.path.basename(file_path)
                print(f"[i] Found existing file: {fname}")
                try:
                    text = extract_text_from_pdf(file_path)
                    print(f"[i] Categorizing manual file: {fname}")
                    print(f"[i] Extracted text preview: {text[:100]}...")
                    category = categorize_invoice(text)
                    sort_file_to_category(file_path, category, text, rename_by_date)
                except Exception as e:
                    print(f"[!] Error processing {fname}: {e}")
    print(f"\n[i] Watching {DOWNLOAD_DIR} for new PDFs and folders using watchdog... (Press Ctrl+C to stop)")
    event_handler = InvoiceHandler(rename_by_date=rename_by_date)
    observer = Observer()
    # Set recursive=True to watch new folders dropped into DOWNLOAD_DIR
    observer.schedule(event_handler, DOWNLOAD_DIR, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n[i] Stopped watching for new PDFs.")
    observer.join()

# -------------- MAIN WORKFLOW --------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-gmail', action='store_true', help='Skip Gmail scanning and only process local PDFs')
    parser.add_argument('--gmail-only', action='store_true', help='Only run Gmail scanning and downloading')
    parser.add_argument('--rename-by-date', action='store_true', help='Rename files using extracted date and category')
    args = parser.parse_args()
    reviewed_ids = load_reviewed_ids()

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(SORTED_DIR, exist_ok=True)

    if not args.no_gmail:
        service = gmail_authenticate()
        search_query = build_search_query(KEYWORDS, TIMEFRAME, START_DATE)
        print(f"[i] Gmail search query: {search_query}")
        messages = search_messages(service, search_query)
        print(f"[i] Found {len(messages)} matching emails.")

        for msg in messages:
            if msg['id'] in reviewed_ids:
                print(f"[→] Skipping already reviewed email ID: {msg['id']}")
                continue
            full_message = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
            subject = "No Subject"
            for header in full_message['payload'].get('headers', []):
                if header['name'] == 'Subject':
                    subject = header['value']
                    break
            sender = ""
            for header in full_message['payload'].get('headers', []):
                if header['name'].lower() == 'from':
                    sender = header['value']
                    break
            if any(blacklisted in sender for blacklisted in BLACKLISTED_SENDERS):
                print(f"[→] Skipping blacklisted sender: {sender}")
                continue
            print(f"\n--- Processing email: {subject} ---")

            for file_path in download_attachments(service, msg['id'], DOWNLOAD_DIR):
                text = extract_text_from_pdf(file_path)
                print(f"[i] Categorizing file: {file_path}")
                print(f"[i] Extracted text preview: {text[:100]}...")
                category = categorize_invoice(text)
                sort_file_to_category(file_path, category, text, args.rename_by_date)

            links = extract_invoice_links_with_ollama(service, msg['id'])
            if links:
                def process_link(link, subject, message_id):
                    file_path_from_link = download_pdf_from_url(link, DOWNLOAD_DIR, subject, message_id)
                    if file_path_from_link:
                        text = extract_text_from_pdf(file_path_from_link)
                        print(f"[i] Categorizing file: {file_path_from_link}")
                        print(f"[i] Extracted text preview: {text[:100]}...")
                        category = categorize_invoice(text)
                        sort_file_to_category(file_path_from_link, category, text, args.rename_by_date)
                        return True
                    return False

                success = False
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(process_link, link, subject, msg['id']) for link in links]
                    for future in as_completed(futures):
                        if future.result():
                            success = True
                if not success:
                    print("[!] All extracted links failed to download.")

    if not args.gmail_only:
        process_dropped_invoices(rename_by_date=args.rename_by_date)

if __name__ == '__main__':
    main()