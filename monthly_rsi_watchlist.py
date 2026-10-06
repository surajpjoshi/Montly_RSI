
import os
import time
import json
import math
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import requests
from dotenv import load_dotenv

# ============================================================
# Monthly RSI >70 Persistent Watchlist
# Stock universe: stock_master(2).xlsx / All Nifty Stocks
# Data source: Upstox V3
#
# Outputs:
#   data/watchlist.xlsx
#   data/cross_history.csv
#   data/hourly_touch_history.csv
#   data/run_log.csv
#
# Key rules:
#   1) New stock is added when completed Monthly RSI crosses
#      from <=70 to >70.
#   2) The crossing price/date are permanently stored.
#   3) Stock remains in watchlist even if Monthly RSI later
#      falls below 70.
#   4) Current Monthly/Weekly/Hourly RSI is calculated using
#      the latest LTP as the close of the currently-forming
#      candle.
#   5) Hourly RSI touch <=30 is counted as one event until
#      RSI first recovers above 30.
#   6) Hourly touch history is preserved with date/time, RSI
#      and LTP.
# ============================================================

load_dotenv()

BASE_URL = "https://api.upstox.com/v3"
TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

MASTER_FILE = os.getenv("STOCK_MASTER_FILE", "stock_master(2).xlsx")
MASTER_SHEET = "All Nifty Stocks"

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

WATCHLIST_FILE = DATA_DIR / "watchlist.xlsx"
CROSS_HISTORY_FILE = DATA_DIR / "cross_history.csv"
TOUCH_HISTORY_FILE = DATA_DIR / "hourly_touch_history.csv"
RUN_LOG_FILE = DATA_DIR / "run_log.csv"

RSI_PERIOD = 14
MONTHLY_THRESHOLD = 70.0
HOURLY_TOUCH_THRESHOLD = 30.0

# Moderate concurrency. Increase only if your Upstox API limits allow it.
WORKERS = int(os.getenv("UPSTOX_WORKERS", "5"))
REQUEST_TIMEOUT = int(os.getenv("UPSTOX_TIMEOUT", "20"))
RETRIES = 3

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
})


def die_if_no_token():
    if not TOKEN:
        raise RuntimeError(
            "UPSTOX_ACCESS_TOKEN is empty. Set it in .env or as an environment variable."
        )


def api_get(path, params=None):
    url = BASE_URL + path
    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("status") == "success":
                    return payload.get("data", {})
                raise RuntimeError(f"Upstox API error: {payload}")

            # Retry rate limiting / transient server errors.
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(attempt * 1.5)
                continue

            raise RuntimeError(f"Upstox HTTP {r.status_code}: {r.text[:500]}")

        except Exception as e:
            last_error = e
            if attempt < RETRIES:
                time.sleep(attempt * 1.5)

    raise RuntimeError(str(last_error))


def candles_to_df(candles):
    if not candles:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
        )

    rows = []
    for c in candles:
        rows.append({
            "timestamp": pd.to_datetime(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]) if len(c) > 5 and c[5] is not None else np.nan,
            "oi": float(c[6]) if len(c) > 6 and c[6] is not None else np.nan,
        })

    df = pd.DataFrame(rows)
    return (
        df.sort_values("timestamp")
          .drop_duplicates("timestamp")
          .reset_index(drop=True)
    )


def fetch_historical(instrument_key, unit, interval, start_date, end_date):
    """
    Upstox V3:
      months/1 and weeks/1 have no practical historical limit.
      hours are limited to roughly one quarter per request, so
      hourly requests are chunked.
    """
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()

    # Keep every request comfortably inside the V3 hourly retrieval window.
    if unit == "hours":
        chunks = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=80), end)
            data = api_get(
                f"/historical-candle/{quote(instrument_key, safe='')}/"
                f"{unit}/{interval}/{chunk_end:%Y-%m-%d}/{cursor:%Y-%m-%d}"
            )
            chunks.extend(data.get("candles", []))
            cursor = chunk_end + timedelta(days=1)
            time.sleep(0.05)
        return candles_to_df(chunks)

    data = api_get(
        f"/historical-candle/{quote(instrument_key, safe='')}/"
        f"{unit}/{interval}/{end:%Y-%m-%d}/{start:%Y-%m-%d}"
    )
    return candles_to_df(data.get("candles", []))


def fetch_intraday_hours(instrument_key):
    data = api_get(
        f"/historical-candle/intraday/{quote(instrument_key, safe='')}/hours/1"
    )
    return candles_to_df(data.get("candles", []))


def fetch_ltp(instrument_keys):
    """
    V3 LTP endpoint accepts instrument_key query values.
    Use batches so the daily run does not make one LTP request
    per stock.
    """
    if not instrument_keys:
        return {}

    result = {}
    # Conservative batch size.
    for i in range(0, len(instrument_keys), 100):
        batch = instrument_keys[i:i + 100]
        # Upstox V3 expects a comma-separated instrument_key parameter.
        params = {"instrument_key": ",".join(batch)}
        data = api_get("/market-quote/ltp", params=params)

        # V3 response keys are display keys such as NSE_EQ:SYMBOL,
        # while instrument_token contains the original NSE_EQ|ISIN key.
        # Map by instrument_token so the scanner can reliably find each LTP.
        for display_key, value in data.items():
            if not isinstance(value, dict):
                continue

            last_price = value.get("last_price", value.get("ltp"))
            instrument_token = value.get("instrument_token")

            if instrument_token and last_price is not None:
                result[str(instrument_token)] = last_price

            if last_price is not None:
                result[str(display_key)] = last_price

        time.sleep(0.05)

    return result


def rsi_wilder(series, period=14):
    """
    Wilder RSI using pandas EWM(alpha=1/period, adjust=False).
    Returns an RSI series aligned with the input.
    """
    s = pd.Series(series, dtype="float64")
    delta = s.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # Standard handling for a no-loss window.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi


def add_live_close(df, ltp, candle="monthly"):
    """
    Add/replace the current unfinished candle with LTP.
    RSI is based on closes, so the current candle's OHLC is
    not required for RSI.
    """
    if df.empty or ltp is None or not np.isfinite(float(ltp)):
        return df.copy()

    out = df.copy()
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    ltp = float(ltp)

    if candle == "monthly":
        key = pd.Timestamp(now.year, now.month, 1, tz="Asia/Kolkata")
    elif candle == "weekly":
        # Monday of current ISO week.
        monday = now.normalize() - pd.Timedelta(days=now.weekday())
        key = monday
    elif candle == "hourly":
        key = now.floor("h")
    else:
        raise ValueError(candle)

    # Normalize timestamp comparison by date/month semantics.
    if candle == "monthly":
        mask = (out["timestamp"].dt.year == key.year) & (out["timestamp"].dt.month == key.month)
    elif candle == "weekly":
        mask = out["timestamp"].dt.to_period("W-MON") == key.to_period("W-MON")
    else:
        mask = out["timestamp"].dt.floor("h") == key

    if mask.any():
        idx = out.index[mask][-1]
        out.loc[idx, "close"] = ltp
    else:
        row = {
            "timestamp": key,
            "open": ltp,
            "high": ltp,
            "low": ltp,
            "close": ltp,
            "volume": np.nan,
            "oi": np.nan,
        }
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)

    return out.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def load_master():
    df = pd.read_excel(MASTER_FILE, sheet_name=MASTER_SHEET)

    required = {"Symbol", "ISIN Code"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing required columns in stock master: {sorted(missing)}")

    df = df.copy()
    df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()
    df["ISIN Code"] = df["ISIN Code"].astype(str).str.strip().str.upper()
    df["instrument_key"] = "NSE_EQ|" + df["ISIN Code"]

    # One row per stock/ISIN, preserving first company metadata.
    df = df[df["Series"].astype(str).str.upper().eq("EQ")].copy()
    df = df.drop_duplicates(subset=["ISIN Code"]).reset_index(drop=True)

    return df


def empty_cross_history():
    return pd.DataFrame(columns=[
        "Stock", "Company Name", "ISIN Code", "Instrument Key",
        "Cross Date", "Cross Price", "Previous Monthly RSI",
        "Cross Monthly RSI", "Detected On"
    ])


def empty_touch_history():
    return pd.DataFrame(columns=[
        "Stock", "Cross Date", "Touch #", "Touch Date",
        "Touch Time", "Hourly RSI", "Touch LTP"
    ])


def load_csv_or_empty(path, empty_factory):
    if path.exists():
        try:
            return pd.read_csv(path)
        except Exception:
            pass
    return empty_factory()


def save_csv(df, path):
    df.to_csv(path, index=False)


def detect_new_monthly_cross(row):
    """
    Confirmed-cross detection uses the latest completed monthly candle.
    We then store that candle's close as Cross Price.

    Current in-progress month is NOT used to create a permanent
    cross event. This prevents a stock from being added and removed
    merely because intramonth RSI moved around 70.
    """
    key = row["instrument_key"]
    end = datetime.now().date()
    start = end - timedelta(days=365 * 3)

    try:
        monthly = fetch_historical(key, "months", 1, start, end)
        if len(monthly) < RSI_PERIOD + 2:
            return None

        # Determine whether the final candle is the current month.
        now = pd.Timestamp.now(tz="Asia/Kolkata")
        latest = monthly.iloc[-1]
        latest_is_current = (
            latest["timestamp"].year == now.year
            and latest["timestamp"].month == now.month
        )

        completed = monthly.iloc[:-1].copy() if latest_is_current else monthly.copy()

        if len(completed) < RSI_PERIOD + 2:
            return None

        completed["rsi"] = rsi_wilder(completed["close"], RSI_PERIOD)
        prev = completed.iloc[-2]
        curr = completed.iloc[-1]

        if (
            pd.notna(prev["rsi"])
            and pd.notna(curr["rsi"])
            and float(prev["rsi"]) <= MONTHLY_THRESHOLD
            and float(curr["rsi"]) > MONTHLY_THRESHOLD
        ):
            return {
                "Stock": row["Symbol"],
                "Company Name": row.get("Company Name", ""),
                "ISIN Code": row["ISIN Code"],
                "Instrument Key": key,
                "Cross Date": pd.Timestamp(curr["timestamp"]).date().isoformat(),
                "Cross Price": float(curr["close"]),
                "Previous Monthly RSI": round(float(prev["rsi"]), 2),
                "Cross Monthly RSI": round(float(curr["rsi"]), 2),
                "Detected On": datetime.now().isoformat(timespec="seconds"),
            }
    except Exception as e:
        print(f"[MONTHLY ERROR] {row['Symbol']}: {e}")

    return None


def update_cross_history(master, history):
    existing_keys = set(history["ISIN Code"].astype(str)) if not history.empty else set()

    candidates = master[~master["ISIN Code"].isin(existing_keys)].copy()

    print(f"Monthly scan: {len(candidates)} stocks not already in cross history.")

    found = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(detect_new_monthly_cross, row): row["Symbol"]
            for _, row in candidates.iterrows()
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
                print(
                    f"[NEW CROSS] {result['Stock']} | "
                    f"{result['Cross Date']} | "
                    f"RSI {result['Cross Monthly RSI']} | "
                    f"Price {result['Cross Price']}"
                )

    if found:
        history = pd.concat([history, pd.DataFrame(found)], ignore_index=True)
        history = history.drop_duplicates(subset=["ISIN Code"], keep="first")

    save_csv(history, CROSS_HISTORY_FILE)
    return history


def completed_and_live_rsi(key, ltp):
    end = datetime.now().date()
    start = end - timedelta(days=365 * 3)

    monthly = fetch_historical(key, "months", 1, start, end)
    weekly = fetch_historical(key, "weeks", 1, start, end)

    # Current live RSI: use LTP as the current unfinished candle close.
    monthly_live = add_live_close(monthly, ltp, "monthly")
    weekly_live = add_live_close(weekly, ltp, "weekly")

    monthly_live["rsi"] = rsi_wilder(monthly_live["close"], RSI_PERIOD)
    weekly_live["rsi"] = rsi_wilder(weekly_live["close"], RSI_PERIOD)

    m_rsi = float(monthly_live.iloc[-1]["rsi"]) if pd.notna(monthly_live.iloc[-1]["rsi"]) else np.nan
    w_rsi = float(weekly_live.iloc[-1]["rsi"]) if pd.notna(weekly_live.iloc[-1]["rsi"]) else np.nan

    # Previous completed monthly RSI.
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    completed_monthly = monthly[
        ~((monthly["timestamp"].dt.year == now.year) &
          (monthly["timestamp"].dt.month == now.month))
    ].copy()

    completed_monthly["rsi"] = rsi_wilder(completed_monthly["close"], RSI_PERIOD)
    prev_m_rsi = (
        float(completed_monthly.iloc[-1]["rsi"])
        if len(completed_monthly) and pd.notna(completed_monthly.iloc[-1]["rsi"])
        else np.nan
    )

    # Hourly: historical candles plus today's intraday candles.
    # Use the last available completed hourly candle for current H-RSI.
    hourly = fetch_historical(
        key, "hours", 1,
        end - timedelta(days=80), end
    )
    try:
        intra = fetch_intraday_hours(key)
        if not intra.empty:
            hourly = pd.concat([hourly, intra], ignore_index=True)
            hourly = (
                hourly.sort_values("timestamp")
                .drop_duplicates("timestamp", keep="last")
                .reset_index(drop=True)
            )
    except Exception:
        pass

    # Current hourly RSI with LTP as live close.
    hourly_live = add_live_close(hourly, ltp, "hourly")
    hourly_live["rsi"] = rsi_wilder(hourly_live["close"], RSI_PERIOD)
    h_rsi = float(hourly_live.iloc[-1]["rsi"]) if pd.notna(hourly_live.iloc[-1]["rsi"]) else np.nan

    return {
        "monthly": monthly_live,
        "weekly": weekly_live,
        "hourly": hourly_live,
        "monthly_rsi": m_rsi,
        "weekly_rsi": w_rsi,
        "hourly_rsi": h_rsi,
        "prev_monthly_rsi": prev_m_rsi,
    }


def touch_events(hourly_df, cross_date):
    """
    A touch is one event where RSI <=30. Consecutive candles at/below
    30 count as ONE event. A new event begins only after RSI recovers >30.
    Only completed candles on/after the crossing date are considered.
    """
    if hourly_df.empty:
        return []

    df = hourly_df.copy()
    df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
    cross_ts = pd.Timestamp(cross_date)

    # Only candles on/after crossing date.
    df = df[df["timestamp"].dt.date >= cross_ts.date()].copy()

    events = []
    in_touch = False
    touch_no = 0

    for _, r in df.iterrows():
        rsi = r["rsi"]
        if pd.isna(rsi):
            continue

        below = float(rsi) <= HOURLY_TOUCH_THRESHOLD

        if below and not in_touch:
            touch_no += 1
            events.append({
                "Touch #": touch_no,
                "Touch Date": pd.Timestamp(r["timestamp"]).date().isoformat(),
                "Touch Time": pd.Timestamp(r["timestamp"]).strftime("%H:%M"),
                "Hourly RSI": round(float(rsi), 2),
            })
            in_touch = True

        elif not below:
            in_touch = False

    return events


def status_m(rsi):
    if pd.isna(rsi):
        return "—"
    return "🟢 >70" if rsi > 70 else "🔴 <70"


def status_w(rsi):
    if pd.isna(rsi):
        return "—"
    return "🟢 >50" if rsi > 50 else "🔴 <50"


def status_h(rsi):
    if pd.isna(rsi):
        return "—"
    if rsi < 30:
        return "🔵 <30"
    if rsi <= 70:
        return "🟢 30–70"
    return "🔴 >70"


def build_watchlist(master, cross_history):
    if cross_history.empty:
        return pd.DataFrame()

    master_idx = master.set_index("ISIN Code")
    keys = cross_history["Instrument Key"].tolist()

    ltps = fetch_ltp(keys)

    touch_history = load_csv_or_empty(
        TOUCH_HISTORY_FILE, empty_touch_history
    )

    rows = []

    for _, cross in cross_history.iterrows():
        isin = cross["ISIN Code"]
        key = cross["Instrument Key"]
        symbol = cross["Stock"]

        try:
            ltp = ltps.get(key)
            if ltp is None:
                print(f"[LTP MISSING] {symbol}")
                continue

            ltp = float(ltp)
            r = completed_and_live_rsi(key, ltp)

            hourly = r["hourly"]
            events = touch_events(hourly, cross["Cross Date"])

            # Rebuild touch rows for this stock from current hourly history.
            old = touch_history[touch_history["Stock"] != symbol].copy()

            new_touch_rows = []
            for ev in events:
                new_touch_rows.append({
                    "Stock": symbol,
                    "Cross Date": cross["Cross Date"],
                    **ev,
                    "Touch LTP": np.nan,
                })

            # Fill touch LTP from hourly candle close.
            if new_touch_rows:
                for tr in new_touch_rows:
                    ts = pd.Timestamp(
                        f"{tr['Touch Date']} {tr['Touch Time']}",
                        tz="Asia/Kolkata"
                    )
                    match = hourly.iloc[
                        (hourly["timestamp"] - ts).abs().argsort()[:1]
                    ]
                    if not match.empty:
                        tr["Touch LTP"] = float(match.iloc[0]["close"])

            touch_history = pd.concat(
                [old, pd.DataFrame(new_touch_rows)],
                ignore_index=True
            )

            touch_history = touch_history.drop_duplicates(
                subset=["Stock", "Cross Date", "Touch #"], keep="last"
            )

            cross_price = float(cross["Cross Price"])
            growth = ((ltp - cross_price) / cross_price) * 100

            # Maximum growth since cross from available daily data.
            # Use daily candles from cross date to current date.
            daily = fetch_historical(
                key, "days", 1,
                pd.Timestamp(cross["Cross Date"]).date(),
                datetime.now().date()
            )
            max_price = float(daily["high"].max()) if not daily.empty else ltp
            max_growth = ((max_price - cross_price) / cross_price) * 100
            drawdown = ((ltp - max_price) / max_price) * 100 if max_price else np.nan

            days_since = (
                datetime.now().date() - pd.Timestamp(cross["Cross Date"]).date()
            ).days

            stock_touch = touch_history[
                touch_history["Stock"].eq(symbol) &
                touch_history["Cross Date"].eq(cross["Cross Date"])
            ].sort_values("Touch #")

            touch_dates = ", ".join(
                f"{x['Touch Date']} {x['Touch Time']}"
                for _, x in stock_touch.iterrows()
            )

            rows.append({
                "Stock": symbol,
                "Company Name": cross["Company Name"],
                "Cross Date": cross["Cross Date"],
                "Cross Price": round(cross_price, 2),
                "LTP": round(ltp, 2),
                "Growth %": round(growth, 2),
                "Max Growth %": round(max_growth, 2),
                "Drawdown %": round(drawdown, 2),
                "Monthly RSI": round(r["monthly_rsi"], 2) if pd.notna(r["monthly_rsi"]) else np.nan,
                "Weekly RSI": round(r["weekly_rsi"], 2) if pd.notna(r["weekly_rsi"]) else np.nan,
                "Hourly RSI": round(r["hourly_rsi"], 2) if pd.notna(r["hourly_rsi"]) else np.nan,
                "Prev Monthly RSI": round(r["prev_monthly_rsi"], 2) if pd.notna(r["prev_monthly_rsi"]) else np.nan,
                "Days Since Cross": days_since,
                "H-RSI Touch ≤30 Count": int(len(stock_touch)),
                "H-RSI Touch Dates": touch_dates,
                "M-RSI Status": status_m(r["monthly_rsi"]),
                "W-RSI Status": status_w(r["weekly_rsi"]),
                "H-RSI Status": status_h(r["hourly_rsi"]),
                "ISIN Code": isin,
                "Instrument Key": key,
            })

            print(
                f"[WATCH] {symbol}: LTP={ltp:.2f}, "
                f"M={r['monthly_rsi']:.2f}, W={r['weekly_rsi']:.2f}, "
                f"H={r['hourly_rsi']:.2f}, "
                f"Touches={len(stock_touch)}, Growth={growth:.2f}%"
            )

        except Exception as e:
            print(f"[WATCH ERROR] {symbol}: {e}")

    save_csv(touch_history, TOUCH_HISTORY_FILE)

    result = pd.DataFrame(rows)

    # GitHub Pages reads this CSV directly. Always write it, including an
    # empty file with the correct headers when no rows can be built.
    if result.empty:
        result = pd.DataFrame(columns=[
            "Stock", "Company Name", "Cross Date", "Cross Price", "LTP",
            "Growth %", "Max Growth %", "Drawdown %", "Monthly RSI",
            "Weekly RSI", "Hourly RSI", "Prev Monthly RSI",
            "Days Since Cross", "H-RSI Touch ≤30 Count", "H-RSI Touch Dates",
            "M-RSI Status", "W-RSI Status", "H-RSI Status",
            "ISIN Code", "Instrument Key"
        ])
        save_csv(result, DATA_DIR / "watchlist.csv")
        return result

    # Useful default sort: strongest post-cross performance first.
    result = result.sort_values(
        ["Growth %", "Monthly RSI"],
        ascending=[False, False]
    ).reset_index(drop=True)

    # Publish the exact table consumed by the GitHub Pages dashboard.
    save_csv(result, DATA_DIR / "watchlist.csv")

    return result


def write_excel(watchlist, cross_history, touch_history):
    with pd.ExcelWriter(WATCHLIST_FILE, engine="openpyxl") as writer:
        watchlist.to_excel(writer, sheet_name="Watchlist", index=False)
        cross_history.to_excel(writer, sheet_name="Cross History", index=False)
        touch_history.to_excel(writer, sheet_name="Hourly Touch History", index=False)

        # Basic formatting.
        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

            for col in ws.columns:
                max_len = 0
                letter = col[0].column_letter
                for cell in col[:1000]:
                    value = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, len(value))
                ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 40)


def append_run_log(status, message):
    row = pd.DataFrame([{
        "Run Time": datetime.now().isoformat(timespec="seconds"),
        "Status": status,
        "Message": message,
    }])
    if RUN_LOG_FILE.exists():
        old = pd.read_csv(RUN_LOG_FILE)
        row = pd.concat([old, row], ignore_index=True)
    row.to_csv(RUN_LOG_FILE, index=False)


def main():
    die_if_no_token()

    print("=" * 70)
    print("MONTHLY RSI >70 PERSISTENT WATCHLIST")
    print("=" * 70)

    master = load_master()
    print(f"Stock master rows: {len(master)}")

    cross_history = load_csv_or_empty(
        CROSS_HISTORY_FILE, empty_cross_history
    )

    cross_history = update_cross_history(master, cross_history)
    print(f"Total crossed stocks stored: {len(cross_history)}")

    watchlist = build_watchlist(master, cross_history)
    touch_history = load_csv_or_empty(
        TOUCH_HISTORY_FILE, empty_touch_history
    )

    if watchlist.empty:
        print("No complete watchlist rows generated.")
        write_excel(
            pd.DataFrame(),
            cross_history,
            touch_history
        )
        save_csv(pd.DataFrame(columns=[
            "Stock", "Company Name", "Cross Date", "Cross Price", "LTP",
            "Growth %", "Max Growth %", "Drawdown %", "Monthly RSI",
            "Weekly RSI", "Hourly RSI", "Prev Monthly RSI",
            "Days Since Cross", "H-RSI Touch ≤30 Count", "H-RSI Touch Dates",
            "M-RSI Status", "W-RSI Status", "H-RSI Status",
            "ISIN Code", "Instrument Key"
        ]), DATA_DIR / "watchlist.csv")
        append_run_log("SUCCESS", "No complete watchlist rows generated.")
        return

    write_excel(watchlist, cross_history, touch_history)

    print("\n" + "=" * 70)
    print("WATCHLIST")
    print("=" * 70)

    display_cols = [
        "Stock", "Cross Date", "Cross Price", "LTP", "Growth %",
        "Monthly RSI", "Weekly RSI", "Hourly RSI",
        "Prev Monthly RSI", "Days Since Cross",
        "H-RSI Touch ≤30 Count", "M-RSI Status",
        "W-RSI Status", "H-RSI Status"
    ]
    print(watchlist[display_cols].to_string(index=False))

    print("\nSaved:")
    print(f"  {WATCHLIST_FILE}")
    print(f"  {CROSS_HISTORY_FILE}")
    print(f"  {TOUCH_HISTORY_FILE}")
    append_run_log("SUCCESS", f"Updated {len(watchlist)} watchlist stocks.")


if __name__ == "__main__":
    main()
