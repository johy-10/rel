import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


PUBLIC_DIR = Path("public")
SCREENSHOT_DIR = PUBLIC_DIR / "screenshots"
DEBUG_DIR = PUBLIC_DIR / "debug"
RATES_FILE = PUBLIC_DIR / "rates.json"

CURRENCIES = ["EUR", "USD"]

PUBLIC_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def make_driver() -> webdriver.Chrome:
    options = Options()

    # Běžné CI nastavení. Není to stealth/bypass konfigurace.
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=cs-CZ")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


def revolut_url(currency: str) -> str:
    lower = currency.lower()
    return f"https://www.revolut.com/cs-CZ/currency-converter/convert-{lower}-to-czk-exchange-rate/"


def is_blocked_or_challenge(text: str, html: str) -> bool:
    haystack = f"{text}\n{html}".lower()

    needles = [
        "just a quick security check",
        "enable javascript and cookies to continue",
        "_cf_chl_opt",
        "cf_chl",
        "challenge-error-text",
        "captcha",
        "access denied",
        "attention required",
        "security check",
    ]

    return any(needle.lower() in haystack for needle in needles)


def normalize_number(value: str) -> float:
    value = value.strip()
    value = value.replace("\u00a0", "").replace(" ", "")

    # 24,65 => 24.65
    if "," in value and "." not in value:
        value = value.replace(",", ".")

    # 1,234.56 => 1234.56
    if re.match(r"^\d{1,3}(,\d{3})+\.\d+$", value):
        value = value.replace(",", "")

    return float(value)


def is_plausible_czk_rate(rate: float, currency: str) -> bool:
    if currency == "EUR":
        return 15 < rate < 40

    if currency == "USD":
        return 10 < rate < 40

    return False


def parse_rate_czk_per_currency(text: str, html: str, currency: str) -> float | None:
    """
    Vrací vždy:
    1 EUR = X CZK
    nebo
    1 USD = X CZK
    """

    currency = currency.upper()

    raw = f"{text}\n{html}"
    raw = re.sub(r"\s+", " ", raw)

    direct_patterns = [
        # 1 EUR = 24.65 CZK
        rf"1\s*{currency}\s*(?:=|is|je|equals?)\s*(?:Kč\s*)?([0-9]+(?:[.,][0-9]+)?)\s*CZK",

        # EUR to CZK ... 24.65
        rf"{currency}\s*(?:to|na|/|-)\s*CZK.{{0,250}}?([0-9]+(?:[.,][0-9]+)?)",

        # 1 Euro = 24.65 Czech Koruna
        rf"1\s*(?:{currency}|euro|us dollar|dollar).{{0,100}}?([0-9]+(?:[.,][0-9]+)?).{{0,100}}?(?:CZK|Czech Koruna|korun)",

        # JSON-like: "from":"EUR" ... "to":"CZK" ... "rate":24.65
        rf'["\'](?:from|base|sourceCurrency|baseCurrency)["\']\s*:\s*["\']{currency}["\'].{{0,800}}?["\'](?:to|quote|targetCurrency|quoteCurrency)["\']\s*:\s*["\']CZK["\'].{{0,800}}?["\'](?:rate|value|exchangeRate)["\']\s*:\s*["\']?([0-9]+(?:[.,][0-9]+)?)',
    ]

    for pattern in direct_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue

        rate = normalize_number(match.group(1))

        if is_plausible_czk_rate(rate, currency):
            return rate

    inverse_patterns = [
        # 1 CZK = 0.0406 EUR
        rf"1\s*CZK\s*(?:=|is|je|equals?)\s*([0-9]+(?:[.,][0-9]+)?)\s*{currency}",

        # CZK to EUR ... 0.0406
        rf"CZK\s*(?:to|na|/|-)\s*{currency}.{{0,250}}?([0-9]+(?:[.,][0-9]+)?)",
    ]

    for pattern in inverse_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue

        inverse = normalize_number(match.group(1))

        if inverse > 0:
            rate = 1 / inverse

            if is_plausible_czk_rate(rate, currency):
                return rate

    return None


def scrape_currency(driver: webdriver.Chrome, currency: str) -> dict:
    url = revolut_url(currency)

    print(f"Opening {url}")
    driver.get(url)

    # Dáme stránce čas na běžný JS render.
    # Neřešíme/neklepeme žádnou challenge.
    time.sleep(12)

    screenshot_path = SCREENSHOT_DIR / f"revolut_{currency}.png"
    html_path = DEBUG_DIR / f"revolut_{currency}.html"

    driver.save_screenshot(str(screenshot_path))

    html = driver.page_source or ""
    html_path.write_text(html, encoding="utf-8", errors="ignore")

    try:
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        text = ""

    if is_blocked_or_challenge(text, html):
        return {
            "currency": currency,
            "url": url,
            "status": "BLOCKED",
            "rate_czk_per_unit": None,
            "screenshot": str(screenshot_path),
            "debug_html": str(html_path),
            "error": "Page returned Cloudflare/security challenge instead of converter content.",
        }

    rate = parse_rate_czk_per_currency(text, html, currency)

    if rate is None:
        return {
            "currency": currency,
            "url": url,
            "status": "PARSE_ERROR",
            "rate_czk_per_unit": None,
            "screenshot": str(screenshot_path),
            "debug_html": str(html_path),
            "error": "Could not parse CZK rate from visible text or HTML.",
        }

    return {
        "currency": currency,
        "url": url,
        "status": "OK",
        "rate_czk_per_unit": rate,
        "screenshot": str(screenshot_path),
        "debug_html": str(html_path),
        "error": None,
    }


def main() -> None:
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Revolut website via Selenium Chrome in GitHub Actions",
        "rates": {},
    }

    driver = make_driver()

    try:
        for currency in CURRENCIES:
            result = scrape_currency(driver, currency)
            output["rates"][currency] = result
            print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        driver.quit()

    RATES_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Saved {RATES_FILE}")


if __name__ == "__main__":
    main()
