#!/usr/bin/env python3
# playwright_search_contracts_fixed.py
# Usage:
#   python playwright_search_contracts_fixed.py input.csv
#   python playwright_search_contracts_fixed.py input.csv --output contracts_with_status.csv --headless --column company_name
#
# Requirements: pandas, playwright
# pip install pandas playwright
# python -m playwright install

import time
import argparse
import signal
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.usaspending.gov/search?hash=924356742dd57817f0e9197e858e75cd"

# Selectors:
INPUT_SELECTOR = "#search"
SUBMIT_SELECTOR = 'button[aria-label="Click to submit your search."]'
RESULT_LABEL_SELECTOR = 'span.filter__dropdown-label'        # expected "Prime Award Results"
NO_RESULTS_SELECTOR = 'p.new-search__no-results-text'        # expected "No results found..."

# Interrupt flag so we can save on Ctrl+C
interrupted = False


def _signal_handler(signum, frame):
    global interrupted
    interrupted = True
    print("\n⚠️  Received interrupt signal — will stop after current row and save progress...")


def run(input_csv: Path, output_csv: Path, column: str, headless: bool, browser_name: str):
    global interrupted

    # Load CSV
    df = pd.read_csv(input_csv, dtype=str)
    if column not in df.columns:
        raise RuntimeError(f"CSV must contain a '{column}' column (columns found: {list(df.columns)})")

    # Ensure a status column exists
    if "status" not in df.columns:
        df["status"] = ""

    # register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    with sync_playwright() as p:
        browser_launcher = {
            "chromium": p.chromium,
            "firefox": p.firefox,
            "webkit": p.webkit
        }.get(browser_name.lower(), p.chromium)

        browser = browser_launcher.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            for idx, row in df.iterrows():
                if interrupted:
                    print("Stopping loop due to interrupt request.")
                    break

                company = str(row.get(column) or "").strip()
                if not company:
                    df.loc[idx, "status"] = "False"
                    print(f"[{idx}] empty {column!r} -> False (saved)")
                    df.to_csv(output_csv, index=False)
                    continue

                print(f"[{idx}] Searching: {company!r}")

                # Navigate (use networkidle to let SPA finish background loading)
                try:
                    page.goto(URL, wait_until="networkidle", timeout=450000)
                except PlaywrightTimeoutError:
                    # fallback
                    print("  ⚠️ networkidle timeout; retrying with domcontentloaded")
                    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    print(f"  ❗ Unexpected navigation error: {e} -> marking False")
                    df.loc[idx, "status"] = "False"
                    df.to_csv(output_csv, index=False)
                    continue

                # Wait for input
                try:
                    page.wait_for_selector(INPUT_SELECTOR, timeout=70000)
                except PlaywrightTimeoutError:
                    print("  ⚠️ Search input not found on page; marking False")
                    df.loc[idx, "status"] = "False"
                    df.to_csv(output_csv, index=False)
                    continue

                # Fill the input (more reliable than clipboard)
                try:
                    page.fill(INPUT_SELECTOR, company, timeout=5000)
                except Exception:
                    # fallback: focus + type
                    try:
                        page.click(INPUT_SELECTOR, timeout=3000)
                    except Exception:
                        try:
                            page.focus(INPUT_SELECTOR)
                        except Exception:
                            pass
                    # select-all then type
                    try:
                        page.keyboard.press("Control+A")
                    except Exception:
                        # mac users might need Meta
                        try:
                            page.keyboard.press("Meta+A")
                        except Exception:
                            pass
                    page.keyboard.type(company, delay=30)

                # short pause for SPA to register the change
                time.sleep(0.2)

                # Press Enter to reveal the Submit button (per page behavior)
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass

                # If Submit appears, click it; otherwise assume Enter triggered results directly
                try:
                    page.wait_for_selector(SUBMIT_SELECTOR, timeout=6000)
                    try:
                        page.click(SUBMIT_SELECTOR, timeout=8000)
                        print("  🔘 Submit clicked.")
                    except PlaywrightTimeoutError:
                        print("  ⚠️ Submit appeared but wasn't clickable.")
                except PlaywrightTimeoutError:
                    print("  ℹ️ Submit did not appear after Enter (maybe results loaded directly).")

                # Wait for either success or no-results
                status_value = "False"
                try:
                    page.wait_for_selector(RESULT_LABEL_SELECTOR, timeout=10000)
                    # verify text
                    try:
                        label_text = page.locator(RESULT_LABEL_SELECTOR).inner_text(timeout=2000).strip()
                    except Exception:
                        label_text = ""
                    if "Prime Award Results" in label_text:
                        status_value = "True"
                        print("  ✅ Results found -> True")
                    else:
                        print(f"  ℹ️ Results label found but text != 'Prime Award Results' ({label_text!r}) -> False")
                        status_value = "False"
                except PlaywrightTimeoutError:
                    # check explicit no-results
                    try:
                        page.wait_for_selector(NO_RESULTS_SELECTOR, timeout=3000)
                        status_value = "False"
                        print("  ❌ No results message -> False")
                    except PlaywrightTimeoutError:
                        print("  ⚠️ Neither result-label nor no-results detected -> False")
                        status_value = "False"
                except Exception as e:
                    print(f"  ❗ Unexpected error while checking results: {e} -> False")
                    status_value = "False"

                # Save status back to dataframe and flush to disk immediately
                df.loc[idx, "status"] = status_value
                try:
                    df.to_csv(output_csv, index=False)
                except Exception as e:
                    print(f"  ❗ Failed to save CSV after row {idx}: {e}")

                # small delay to avoid too-fast requests
                time.sleep(0.6)

        finally:
            # Ensure we close context and browser
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print(f"\nDone. Results written to: {output_csv}")


def parse_args():
    ap = argparse.ArgumentParser(description="Search USASpending for company names and mark whether Prime Award Results exist.")
    ap.add_argument("input_csv", type=Path, help="Input CSV file (must contain a column with company names).")
    ap.add_argument("--output", "-o", type=Path, default=Path("contracts_with_status.csv"),
                    help="Output CSV file to write results to (default: contracts_with_status.csv).")
    ap.add_argument("--column", "-c", type=str, default="company_name",
                    help="Column name in the CSV containing company names (default: company_name).")
    ap.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium",
                    help="Which browser engine to use (default: chromium).")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not args.input_csv.exists():
        print(f"Input CSV not found: {args.input_csv}")
        raise SystemExit(1)

    try:
        run(input_csv=args.input_csv, output_csv=args.output, column=args.column,
            headless=args.headless, browser_name=args.browser)
    except Exception as exc:
        print(f"Fatal error: {exc}")
        raise
