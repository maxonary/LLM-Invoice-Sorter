import os
import re
import fitz
import openai
import ollama
import pandas as pd
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

REPORTS_DIR = "Reports"
MODEL = "mistral"
USE_OPENAI_KEY = os.getenv("USE_OPENAI", False)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"

LLM_CACHE_FILE = "llm_cache.json"
try:
    with open(LLM_CACHE_FILE, "r") as f:
        LLM_CACHE = json.load(f)
except FileNotFoundError:
    LLM_CACHE = {}

def cache_key(text, language):
    return hashlib.sha256((text + language).encode()).hexdigest()

def get_column_mapping(language):
    return {
        "date": "Datum" if language == "de" else "Date",
        "location": "Ort" if language == "de" else "Location",
        "purpose": "Anlass" if language == "de" else "Purpose",
        "duration": "Dauer in Std." if language == "de" else "Duration (hrs)",
        "distance_km": "(Teil-) Anfahrt in km" if language == "de" else "Distance (km)",
        "parking": "Parken" if language == "de" else "Parking",
        "hotel": "Hotel",
        "transport": "Zug, Flug, Taxi, ÖPNV" if language == "de" else "Transport",
        "meal": "Bewirtung" if language == "de" else "Meal",
        "fee": "Gebühr" if language == "de" else "Fee",
        "file_paths": "Dateipfade" if language == "de" else "File paths"
    }

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


# Unified LLM function for extracting description, distance, and type
def generate_llm_fields(text, category, event=None, language='en'):
    prompt = f"""
You are a tax assistant helping to analyze receipts.

Task:
1. Summarize the purpose of the expense in 5–10 words.
2. Estimate one-way travel distance in kilometers if relevant, else return 0.
3. Identify what category this amount belongs to (Parking, Hotel, Public Transport, Meal, Fee, etc.).

Respond in JSON with keys: "anlass", "distance_km", and "type".
Respond in {{"German" if language == "de" else "English"}}.

Invoice content:
{text}
"""
    if event:
        prompt += f"\n\nCalendar context: {event}"
    if USE_OPENAI_KEY:
        response = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100
        )
        import json
        try:
            return json.loads(response.choices[0].message['content'])
        except:
            return {"anlass": "", "distance_km": 0, "type": ""}
    else:
        response = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
        import json
        try:
            return json.loads(response['message']['content'])
        except:
            return {"anlass": "", "distance_km": 0, "type": ""}

def generate_travel_report(year, sorted_dir, calendar_context, force_include=False, language='en', use_cache=False, use_parallel=False):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    processed_count = 0
    skipped_count = 0

    # 1. Insert ExcelWriter setup at function start after os.makedirs
    from openpyxl import load_workbook
    from pandas import ExcelWriter
    import tempfile

    excel_temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    writer = ExcelWriter(excel_temp_file.name, engine='openpyxl')
    current_row = 0

    entries_by_date = {}

    column_map = get_column_mapping(language)
    columns = list(column_map.values())

    def process_invoice(path, file, category, year, calendar_context, force_include, language):
        text = extract_text_from_pdf(path)
        date = extract_date(text)
        if not date:
            date_from_filename = re.search(r'(\d{4})[.\-_](\d{1,2})[.\-_](\d{1,2})', file)
            if date_from_filename:
                y, m, d = date_from_filename.groups()
                date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        if not date:
            if not force_include:
                return None, f"[!] Skipping {file}: no date found"
            else:
                date = f"{year}-01-01"

        if not date.startswith(str(year)):
            if not force_include:
                return None, f"[!] Skipping {file}: no matching date found for year {year}"
            else:
                date = f"{year}-01-01"

        amount = extract_amount(text) or ""
        event = None
        if calendar_context and date in calendar_context:
            event = ", ".join(calendar_context[date])
        key = cache_key(text, language)
        if use_cache and key in LLM_CACHE:
            llm_data = LLM_CACHE[key]
        else:
            llm_data = generate_llm_fields(text, category, event, language)
            if use_cache:
                LLM_CACHE[key] = llm_data
                with open(LLM_CACHE_FILE, "w") as f:
                    json.dump(LLM_CACHE, f)
        type_hint = llm_data.get("type", "").lower()
        entry = {
            "date": date,
            "location": "",
            "purpose": llm_data.get("anlass", event or ""),
            "duration": 10 if category == "Travel" else "",
            "distance_km": llm_data.get("distance_km", "") if category == "Travel" else "",
            "parking": amount if "park" in type_hint else "",
            "hotel": amount if "hotel" in type_hint else "",
            "transport": amount if ("transport" in type_hint or "taxi" in type_hint or "bahn" in type_hint) else "",
            "meal": amount if category == "Food" else "",
            "fee": amount if "fee" in type_hint else "",
            "file_paths": os.path.relpath(path)
        }
        print(f"[•] Processed {file} ({category}) → Date: {date}")
        return (date, entry), None

    if use_parallel:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for category in ["Travel", "Food"]:
                dir_path = os.path.join(sorted_dir, category)
                if not os.path.isdir(dir_path):
                    continue
                for file in os.listdir(dir_path):
                    if not file.lower().endswith(".pdf"):
                        continue
                    path = os.path.join(dir_path, file)
                    futures.append(executor.submit(process_invoice, path, file, category, year, calendar_context, force_include, language))

            for future in as_completed(futures):
                try:
                    result, warning = future.result()
                except Exception as e:
                    print(f"[!] Threaded processing error: {e}")
                    skipped_count += 1
                    continue
                if warning:
                    print(warning)
                    skipped_count += 1
                    continue
                if result:
                    date, entry = result
                    entries_by_date.setdefault(date, []).append(entry)
                    processed_count += 1
    else:
        for category in ["Travel", "Food"]:
            dir_path = os.path.join(sorted_dir, category)
            if not os.path.isdir(dir_path):
                continue
            for file in os.listdir(dir_path):
                if not file.lower().endswith(".pdf"):
                    continue
                path = os.path.join(dir_path, file)
                result, warning = process_invoice(path, file, category, year, calendar_context, force_include, language)
                if warning:
                    print(warning)
                    skipped_count += 1
                    continue
                if result:
                    date, entry = result
                    entries_by_date.setdefault(date, []).append(entry)
                    processed_count += 1

    # Filter and link only days that include Travel (based on new structure: use "duration" as indicator)
    filtered_entries = {}
    for date, entries in entries_by_date.items():
        travel_entry = next((e for e in entries if e.get("duration") == 10), None)
        if not travel_entry:
            print(f"[!] Skipping {date}: no Travel entry found")
            continue
        for entry in entries:
            if entry.get("meal"):
                entry["purpose"] = travel_entry["purpose"]
                entry["location"] = travel_entry["location"]
        filtered_entries[date] = entries

    if not filtered_entries:
        print("[!] No valid travel entries found. Report will be empty.")

    # 2. Write directly to Excel as rows are processed
    for date in sorted(filtered_entries.keys()):
        # sort: travel entry (duration==10) first
        daily_entries = sorted(filtered_entries[date], key=lambda e: e.get("duration", "") != 10)
        if not daily_entries:
            continue
        merged = daily_entries[0]
        for extra in daily_entries[1:]:
            for key in ["parking", "hotel", "transport", "meal", "fee"]:
                if extra.get(key):
                    try:
                        merged[key] = str(float(merged.get(key, 0)) + float(extra[key]))
                    except:
                        merged[key] = extra[key]
            merged["file_paths"] += f"\n{extra['file_paths']}"
        df_row = pd.DataFrame([{column_map[k]: v for k, v in merged.items()}], columns=columns)
        df_row.to_excel(writer, index=False, header=(current_row == 0), startrow=current_row)
        current_row += 1

    # 3. Finalize ExcelWriter and print output
    writer.close()
    if language == 'de':
        final_path = os.path.join(REPORTS_DIR, f"reisekosten_{year}_de.xlsx")
    else:
        final_path = os.path.join(REPORTS_DIR, f"travel_report_{year}_en.xlsx")
    import shutil
    shutil.move(excel_temp_file.name, final_path)
    print(f"[✓] Travel report generated: {final_path}")
    print(f"[✓] Processed entries: {processed_count}")
    print(f"[•] Skipped files: {skipped_count}")