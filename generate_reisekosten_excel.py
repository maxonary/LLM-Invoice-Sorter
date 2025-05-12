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


# Unified LLM function for extracting description, distance, and type
def generate_llm_fields(text, category, event=None):
    prompt = f"""
You are a tax assistant helping to analyze receipts.

Task:
1. Summarize the purpose of the expense in 5–10 words.
2. Estimate one-way travel distance in kilometers if relevant, else return 0.
3. Identify what category this amount belongs to (Parking, Hotel, Public Transport, Meal, Fee, etc.).

Respond in JSON with keys: "anlass", "distance_km", and "type".

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

def generate_travel_report(year, sorted_dir, calendar_context, force_include=False, language='en'):
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

    # Default to English column names
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

            if not date:
                if not force_include:
                    print(f"[!] Skipping {file}: no date found")
                    skipped_count += 1
                    continue
                else:
                    date = f"{year}-01-01"
                    print(f"[!] No date found in {file}, using fallback: {date}")

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
            # Unified LLM call
            llm_data = generate_llm_fields(text, category, event)
            type_hint = llm_data.get("type", "").lower()
            entry = {
                "Date": date,
                "Location": "",
                "Purpose": llm_data.get("anlass", event or ""),
                "Duration (hrs)": 10 if category == "Travel" else "",
                "Distance (km)": llm_data.get("distance_km", "") if category == "Travel" else "",
                "Parking": amount if "park" in type_hint else "",
                "Hotel": amount if "hotel" in type_hint else "",
                "Transport": amount if ("transport" in type_hint or "taxi" in type_hint or "bahn" in type_hint) else "",
                "Meal": amount if category == "Food" else "",
                "Fee": amount if "fee" in type_hint else "",
                "File paths": os.path.relpath(path)
            }
            entries_by_date.setdefault(date, []).append(entry)
            processed_count += 1

    # Filter and link only days that include Travel (based on new structure: use "Duration (hrs)" as indicator)
    filtered_entries = {}
    for date, entries in entries_by_date.items():
        travel_entry = next((e for e in entries if e.get("Duration (hrs)") == 10), None)
        if not travel_entry:
            print(f"[!] Skipping {date}: no Travel entry found")
            continue
        for entry in entries:
            if entry.get("Meal"):
                entry["Purpose"] = travel_entry["Purpose"]
                entry["Location"] = travel_entry["Location"]
        filtered_entries[date] = entries

    # 2. Write directly to Excel as rows are processed
    columns = [
        "Date", "Location", "Purpose", "Duration (hrs)", "Distance (km)",
        "Parking", "Hotel", "Transport", "Meal", "Fee", "File paths"
    ]
    for date in sorted(filtered_entries.keys()):
        daily_entries = sorted(filtered_entries[date], key=lambda e: e.get("Duration (hrs)", "") != 10)
        if not daily_entries:
            continue
        merged = daily_entries[0]
        for extra in daily_entries[1:]:
            for key in ["Parking", "Hotel", "Transport", "Meal", "Fee"]:
                if extra.get(key):
                    try:
                        merged[key] = str(float(merged.get(key, 0)) + float(extra[key]))
                    except:
                        merged[key] = extra[key]
            merged["File paths"] += f"\n{extra['File paths']}"
        df_row = pd.DataFrame([merged], columns=columns)
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