import os
import re
import fitz
import openai
import ollama
import pandas as pd

REPORTS_DIR = "Reports"
MODEL = "mistral"
USE_OPENAI_KEY = os.getenv("USE_OPENAI", False)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"

if USE_OPENAI_KEY and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

def extract_text_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    return "\n".join(page.get_text() for page in doc)[:2000]

def extract_date(text):
    match = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None

def extract_amount(text):
    match = re.search(r'(\d{1,4}[,.]\d{2}) ?€', text)
    if match:
        return match.group(1).replace(',', '.')
    return None

def generate_llm_description(text, category, event=None):
    prompt = f"Summarize this {category.lower()} invoice in 5–10 words for a tax report. Focus on purpose, trip, or dining context."
    if event:
        prompt += f" Event context: {event}."
    prompt += f"\n\n{text}"
    if USE_OPENAI_KEY:
        response = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=25
        )
        return response.choices[0].message['content'].strip()
    else:
        response = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
        return response['message']['content'].strip()

def generate_travel_report(year, sorted_dir, calendar_context, force_include=False):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    data = []
    processed_count = 0
    skipped_count = 0

    for category in ["Travel", "Food"]:
        dir_path = os.path.join(sorted_dir, category)
        if not os.path.isdir(dir_path):
            continue

        for file in os.listdir(dir_path):
            print(f"[•] Checking file: {file} in category: {category}")
            if not file.lower().endswith(".pdf"):
                continue

            path = os.path.join(dir_path, file)
            text = extract_text_from_pdf(path)
            date = extract_date(text)
            if not date:
                date_from_filename = re.search(r'(\d{4})[.\-_](\d{1,2})[.\-_](\d{1,2})', file)
                if date_from_filename:
                    y, m, d = date_from_filename.groups()
                    date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

            # Handle missing date with force_include
            if not date:
                if not force_include:
                    print(f"[!] Skipping {file}: no date found")
                    skipped_count += 1
                    continue
                else:
                    date = f"{year}-01-01"
                    print(f"[!] No date found in {file}, using fallback: {date}")
            # Handle year mismatch with force_include
            if not date.startswith(str(year)):
                if not force_include:
                    print(f"[!] Skipping {file}: no matching date found for year {year}")
                    skipped_count += 1
                    continue
                else:
                    print(f"[!] Date in {file} does not match year {year}, using fallback: {year}-01-01")
                    date = f"{year}-01-01"

            amount = extract_amount(text) or ""
            event = None
            if calendar_context and date in calendar_context:
                event = ", ".join(calendar_context[date])

            description = generate_llm_description(text, category, event)
            # Estimate Verpflegungsmehraufwand based on dummy duration (assumed 10h here)
            duration_hours = 10  # This could be extracted more precisely in future
            if duration_hours >= 24:
                vma = 28
            elif duration_hours >= 8:
                vma = 14
            else:
                vma = 0
            processed_count += 1
            data.append({
                "Datum": date,
                "Ort": "",  # Optional – can be extracted later if needed
                "Anlass": event or "",
                "Kategorie": category,
                "Beschreibung": description,
                "Betrag (€)": amount,
                "Verpflegungsmehraufwand (€)": vma,
                "Dateipfad": os.path.relpath(path)
            })

    df = pd.DataFrame(data)
    out_path = os.path.join(REPORTS_DIR, f"reisekosten_{year}.xlsx")
    df.to_excel(out_path, index=False)
    print(f"[✓] Travel report generated: {out_path}")
    print(f"[✓] Processed entries: {processed_count}")
    print(f"[•] Skipped files: {skipped_count}")