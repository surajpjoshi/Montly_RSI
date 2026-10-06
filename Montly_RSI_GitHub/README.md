# Monthly RSI >70 Persistent Watchlist

GitHub Pages dashboard + Upstox V3 scanner for tracking stocks whose **completed monthly RSI(14) crosses above 70**.

## Included files

```text
Montly_RSI/
├── index.html
├── styles.css
├── app.js
├── monthly_rsi_watchlist.py
├── stock_master(2).xlsx
├── requirements.txt
├── .env.example
├── data/
│   └── watchlist.csv
└── .github/workflows/
    ├── update_watchlist.yml
    └── pages.yml
```

## 1. Upload to GitHub

Upload all files/folders to the `master` branch of your `Montly_RSI` repository. Keep the folder names exactly as shown.

## 2. Add the Upstox token as a GitHub Secret

Repository → **Settings → Secrets and variables → Actions → New repository secret**

Name:

```text
UPSTOX_ACCESS_TOKEN
```

Value: your current Upstox access token.

Do **not** put the token inside Python, HTML, JavaScript, `.env`, or this README.

## 3. Enable GitHub Pages

Repository → **Settings → Pages** → Source: **GitHub Actions**.

The included `pages.yml` workflow deploys the dashboard whenever code is pushed to `master`.

## 4. Run the scanner manually once

Go to **Actions → Update Monthly RSI Watchlist → Run workflow**.

The workflow will:

1. Install Python dependencies.
2. Read `stock_master(2).xlsx` / `All Nifty Stocks`.
3. Scan Upstox V3 data.
4. Detect confirmed monthly RSI crosses above 70.
5. Preserve the historical crossing date and price.
6. Calculate current LTP, Monthly/Weekly/Hourly RSI and growth.
7. Track Hourly RSI <=30 touch events.
8. Update `data/watchlist.csv`.
9. Commit the updated CSV.
10. GitHub Pages automatically redeploys the dashboard.

## 5. Automatic schedule

The scanner is scheduled for **7:30 PM IST Monday-Friday**.

GitHub Actions uses UTC, so the workflow contains:

```text
0 14 * * 1-5
```

You can also run it manually from the Actions tab.

## Scanner rules

### Monthly RSI crossing

A permanent cross is created only when:

```text
Previous completed Monthly RSI <= 70
AND
Current completed Monthly RSI > 70
```

The current unfinished month cannot create a permanent cross.

### Persistent watchlist

Once a stock crosses above 70, it remains in the watchlist even if its Monthly RSI later falls below 70.

### Live RSI

The current LTP is used as the close of the currently-forming Monthly, Weekly and Hourly candle to estimate the live RSI.

### Hourly RSI touch

An Hourly RSI <=30 starts one touch event. Consecutive candles <=30 remain one event. A new event starts only after RSI recovers above 30 and later returns to <=30.

## Local Windows run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `.env` from `.env.example` and add your token, then:

```powershell
python monthly_rsi_watchlist.py
```

## Important

The Upstox token should always be stored as a GitHub Actions Secret. Never commit a real token to the repository.
