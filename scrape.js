//scraper

const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

const LOCALE = 'cs-CZ';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';

const PAIRS = [
  ['CZK', 'EUR'],
  ['EUR', 'CZK'],
  ['CZK', 'USD'],
  ['USD', 'CZK'],
];

const UPLOAD_URL = process.env.WEDOS_UPLOAD_URL || '';
const UPLOAD_TOKEN = process.env.WEDOS_UPLOAD_TOKEN || '';

function pageUrl(from, to) {
  return `https://www.revolut.com/${LOCALE}/currency-converter/convert-${from.toLowerCase()}-to-${to.toLowerCase()}-exchange-rate/`;
}
function dataUrl(buildId, from, to) {
  return `https://www.revolut.com/_next/data/${buildId}/${LOCALE}/currency-converter/convert-${from.toLowerCase()}-to-${to.toLowerCase()}-exchange-rate.json`;
}

function extractFromWidget(widget, from, to, sourceUrl) {
  const rate = widget?.rate?.rate;
  if (typeof rate !== 'number') return null;
  return {
    from, to,
    rate,
    senderAmount: widget?.senderAmount ?? null,
    senderCurrency: widget?.senderCurrency ?? null,
    recipientAmount: widget?.recipientAmount ?? null,
    recipientCurrency: widget?.recipientCurrency ?? null,
    sourceUrl,
  };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ locale: LOCALE, userAgent: UA, viewport: { width: 1366, height: 900 } });
  const page = await ctx.newPage();

  const rates = {};
  const errors = [];
  let buildId = null;

  const [f0, t0] = PAIRS[0];
  const firstUrl = pageUrl(f0, t0);
  try {
    const resp = await page.goto(firstUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    const status = resp ? resp.status() : 0;

    await page.waitForTimeout(8000);

    const nextText = await page.locator('#__NEXT_DATA__').textContent().catch(() => null);
    if (!nextText) throw new Error(`__NEXT_DATA__ nenalezen (status ${status}) – challenge možná neprošla`);

    const nextData = JSON.parse(nextText);
    buildId = nextData.buildId || null;
    if (!buildId) throw new Error('buildId nenalezen v __NEXT_DATA__');

    const widget = nextData?.props?.pageProps?.widgetData?.['exchange-rates-widget'];
    const row = extractFromWidget(widget, f0, t0, firstUrl);
    if (row) rates[`${f0}_${t0}`] = row;
    else errors.push({ pair: `${f0}_${t0}`, error: 'rate nenalezen na první stránce' });
  } catch (e) {
    errors.push({ pair: `${f0}_${t0}`, error: e.message });
  }

  if (buildId) {
    for (const [from, to] of PAIRS.slice(1)) {
      const url = dataUrl(buildId, from, to);
      try {

        const json = await page.evaluate(async (u) => {
          const r = await fetch(u, { headers: { 'Accept': 'application/json' } });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return await r.json();
        }, url);

        const widget = json?.pageProps?.widgetData?.['exchange-rates-widget'];
        const row = extractFromWidget(widget, from, to, url);
        if (row) rates[`${from}_${to}`] = row;
        else errors.push({ pair: `${from}_${to}`, error: 'rate nenalezen v _next/data' });
      } catch (e) {
        errors.push({ pair: `${from}_${to}`, error: e.message });
      }
      await page.waitForTimeout(1000 + Math.random() * 1500);
    }
  }

  await browser.close();

  const output = {
    provider: 'Revolut',
    source: 'revolut-next-data-stealth',
    buildId,
    fetchedAt: new Date().toISOString(),
    okCount: Object.keys(rates).length,
    errorCount: errors.length,
    rates,
    errors,
  };

  const fs = require('fs');
  fs.writeFileSync('latest_rates.json', JSON.stringify(output, null, 2));
  console.log(JSON.stringify(output, null, 2));


  if (Object.keys(rates).length === 0) {
    console.error('Žádný kurz se nepodařilo načíst – upload se přeskočí, aby se na Wedosu nepřepsala dobrá data prázdnými.');
    process.exit(1);
  }
  if (!UPLOAD_URL || !UPLOAD_TOKEN) {
    console.error('WEDOS_UPLOAD_URL nebo WEDOS_UPLOAD_TOKEN chybí – upload se přeskočí.');
    process.exit(1);
  }

  try {
    const body = JSON.stringify(output);
    const res = await fetch(UPLOAD_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': UPLOAD_TOKEN },
      body,
    });
    const text = await res.text();
    console.log(`Upload HTTP ${res.status}: ${text.slice(0, 300)}`);
    if (!res.ok) process.exit(1);
  } catch (e) {
    console.error('Upload selhal: ' + e.message);
    process.exit(1);
  }
})().catch((e) => {
  console.error('Fatální chyba: ' + e.message);
  process.exit(1);
});
