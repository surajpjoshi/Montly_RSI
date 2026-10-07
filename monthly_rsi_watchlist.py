import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import requests
from dotenv import load_dotenv


# ============================================================
# Monthly RSI >70 Active Watchlist
#
# Stock universe:
#   stock_master(2).xlsx
#   Sheet: All Nifty Stocks
#
# Data source:
#   Upstox V3
#
# Outputs:
#   data/watchlist.xlsx
#   data/watchlist.csv
#   data/cross_history.csv
#   data/hourly_touch_history.csv
#   data/run_log.csv
#
#
# IMPORTANT WATCHLIST LOGIC
# ============================================================
#
# 1. Historical Monthly RSI cross:
#
#    Completed Monthly RSI:
#
#       Previous Monthly RSI <= 70
#       Current Monthly RSI  > 70
#
#    is permanently stored in cross_history.csv.
#
#
# 2. ACTIVE WATCHLIST:
#
#    Current LIVE Monthly RSI > 70
#
#    This includes BOTH:
#
#       A) HISTORICAL CROSS
#          Original confirmed cross exists in cross_history.csv.
#
#       B) ALREADY ABOVE 70
#          Stock is currently above 70 but its original
#          historical cross was not captured by this scanner.
#
#
# 3. WEEKLY RSI:
#
#    Calculated for BOTH active stock types.
#
#
# 4. HOURLY RSI:
#
#    Calculated for BOTH active stock types.
#
#
# 5. HOURLY RSI TOUCH:
#
#    RSI <= 30 starts a touch event.
#
#    Consecutive candles <= 30 count as ONE touch.
#
#    A new touch starts only after RSI recovers above 30.
#
#
# 6. CROSS DATE / CROSS PRICE:
#
#    Only populated when a real historical cross exists.
#
#    We NEVER invent Cross Date or Cross Price for an
#    ALREADY ABOVE 70 stock.
#
#
# 7. GROWTH:
#
#    Growth / Max Growth / Drawdown are calculated only
#    when a real historical Cross Price exists.
#
# ============================================================


load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://api.upstox.com/v3"

TOKEN = os.getenv(
    "UPSTOX_ACCESS_TOKEN",
    ""
).strip()


MASTER_FILE = os.getenv(
    "STOCK_MASTER_FILE",
    "stock_master(2).xlsx"
)

MASTER_SHEET = "All Nifty Stocks"


DATA_DIR = Path("data")
DATA_DIR.mkdir(
    exist_ok=True
)


WATCHLIST_FILE = (
    DATA_DIR / "watchlist.xlsx"
)

WATCHLIST_CSV_FILE = (
    DATA_DIR / "watchlist.csv"
)

CROSS_HISTORY_FILE = (
    DATA_DIR / "cross_history.csv"
)

TOUCH_HISTORY_FILE = (
    DATA_DIR / "hourly_touch_history.csv"
)

RUN_LOG_FILE = (
    DATA_DIR / "run_log.csv"
)


# ============================================================
# RSI SETTINGS
# ============================================================

RSI_PERIOD = 14

MONTHLY_THRESHOLD = 70.0

HOURLY_TOUCH_THRESHOLD = 30.0


# ============================================================
# API SETTINGS
# ============================================================

WORKERS = int(
    os.getenv(
        "UPSTOX_WORKERS",
        "5"
    )
)

REQUEST_TIMEOUT = int(
    os.getenv(
        "UPSTOX_TIMEOUT",
        "20"
    )
)

RETRIES = 3


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({

    "Accept":
        "application/json",

    "Content-Type":
        "application/json",

    "Authorization":
        f"Bearer {TOKEN}",
})


# ============================================================
# TOKEN CHECK
# ============================================================

def die_if_no_token():

    if not TOKEN:

        raise RuntimeError(
            "UPSTOX_ACCESS_TOKEN is empty. "
            "Set it in .env or as an environment variable."
        )


# ============================================================
# UPSTOX API GET
# ============================================================

def api_get(
    path,
    params=None
):

    url = BASE_URL + path

    last_error = None

    for attempt in range(
        1,
        RETRIES + 1
    ):

        try:

            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 200:

                payload = response.json()

                if payload.get("status") == "success":

                    return payload.get(
                        "data",
                        {}
                    )

                raise RuntimeError(
                    f"Upstox API error: {payload}"
                )


            # ------------------------------------------------
            # Retry temporary errors
            # ------------------------------------------------

            if response.status_code in (
                429,
                500,
                502,
                503,
                504
            ):

                time.sleep(
                    attempt * 1.5
                )

                continue


            raise RuntimeError(
                f"Upstox HTTP "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )


        except Exception as e:

            last_error = e

            if attempt < RETRIES:

                time.sleep(
                    attempt * 1.5
                )


    raise RuntimeError(
        str(last_error)
    )


# ============================================================
# CONVERT UPSTOX CANDLES TO DATAFRAME
# ============================================================

def candles_to_df(candles):

    if not candles:

        return pd.DataFrame(
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "oi"
            ]
        )


    timestamps = pd.to_datetime(
        [c[0] for c in candles],
        utc=True
    ).tz_convert(
        "Asia/Kolkata"
    )


    rows = []


    for i, candle in enumerate(candles):

        rows.append({

            "timestamp":
                timestamps[i],

            "open":
                float(candle[1]),

            "high":
                float(candle[2]),

            "low":
                float(candle[3]),

            "close":
                float(candle[4]),

            "volume":
                (
                    float(candle[5])
                    if len(candle) > 5
                    and candle[5] is not None
                    else np.nan
                ),

            "oi":
                (
                    float(candle[6])
                    if len(candle) > 6
                    and candle[6] is not None
                    else np.nan
                ),
        })


    df = pd.DataFrame(
        rows
    )


    df["timestamp"] = (
        pd.to_datetime(
            df["timestamp"],
            utc=True
        )
        .dt
        .tz_convert("Asia/Kolkata")
    )


    return (
        df
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )


# ============================================================
# FETCH HISTORICAL CANDLES
# ============================================================

def fetch_historical(
    instrument_key,
    unit,
    interval,
    start_date,
    end_date
):

    start = pd.Timestamp(
        start_date
    ).date()

    end = pd.Timestamp(
        end_date
    ).date()


    # --------------------------------------------------------
    # Hourly candles require chunking
    # --------------------------------------------------------

    if unit == "hours":

        chunks = []

        cursor = start


        while cursor <= end:

            chunk_end = min(
                cursor + timedelta(days=80),
                end
            )


            data = api_get(

                f"/historical-candle/"
                f"{quote(instrument_key, safe='')}/"
                f"{unit}/"
                f"{interval}/"
                f"{chunk_end:%Y-%m-%d}/"
                f"{cursor:%Y-%m-%d}"

            )


            chunks.extend(
                data.get(
                    "candles",
                    []
                )
            )


            cursor = (
                chunk_end
                + timedelta(days=1)
            )


            time.sleep(
                0.05
            )


        return candles_to_df(
            chunks
        )


    # --------------------------------------------------------
    # Monthly / Weekly / Daily
    # --------------------------------------------------------

    data = api_get(

        f"/historical-candle/"
        f"{quote(instrument_key, safe='')}/"
        f"{unit}/"
        f"{interval}/"
        f"{end:%Y-%m-%d}/"
        f"{start:%Y-%m-%d}"

    )


    return candles_to_df(
        data.get(
            "candles",
            []
        )
    )


# ============================================================
# FETCH INTRADAY HOURLY
# ============================================================

def fetch_intraday_hours(
    instrument_key
):

    data = api_get(

        f"/historical-candle/intraday/"
        f"{quote(instrument_key, safe='')}/"
        f"hours/1"

    )


    return candles_to_df(
        data.get(
            "candles",
            []
        )
    )


# ============================================================
# FETCH LTP
# ============================================================

def fetch_ltp(
    instrument_keys
):

    if not instrument_keys:

        return {}


    result = {}


    # --------------------------------------------------------
    # Batch LTP requests
    # --------------------------------------------------------

    for i in range(
        0,
        len(instrument_keys),
        100
    ):

        batch = instrument_keys[
            i:i + 100
        ]


        params = {

            "instrument_key":
                ",".join(batch)

        }


        data = api_get(
            "/market-quote/ltp",
            params=params
        )


        # ----------------------------------------------------
        # Map using instrument_token
        # ----------------------------------------------------

        for display_key, value in data.items():

            if not isinstance(
                value,
                dict
            ):

                continue


            last_price = value.get(
                "last_price",
                value.get("ltp")
            )


            instrument_token = value.get(
                "instrument_token"
            )


            if (
                instrument_token
                and
                last_price is not None
            ):

                result[
                    str(instrument_token)
                ] = last_price


            if last_price is not None:

                result[
                    str(display_key)
                ] = last_price


        time.sleep(
            0.05
        )


    return result


# ============================================================
# WILDER RSI
# ============================================================

def rsi_wilder(
    series,
    period=14
):

    s = pd.Series(
        series,
        dtype="float64"
    )


    delta = s.diff()


    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )


    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )


    rsi = (
        100 -
        (
            100 /
            (1 + rs)
        )
    )


    # No-loss window
    rsi = rsi.where(
        avg_loss != 0,
        100.0
    )


    # No movement
    rsi = rsi.where(
        ~(
            (avg_gain == 0)
            &
            (avg_loss == 0)
        ),
        50.0
    )


    return rsi


# ============================================================
# ADD CURRENT LIVE LTP TO CANDLE DATA
# ============================================================

def add_live_close(
    df,
    ltp,
    candle="monthly"
):

    if (
        df.empty
        or
        ltp is None
        or
        not np.isfinite(
            float(ltp)
        )
    ):

        return df.copy()


    out = df.copy()


    now = pd.Timestamp.now(
        tz="Asia/Kolkata"
    )


    ltp = float(
        ltp
    )


    # --------------------------------------------------------
    # Current candle key
    # --------------------------------------------------------

    if candle == "monthly":

        key = pd.Timestamp(
            now.year,
            now.month,
            1,
            tz="Asia/Kolkata"
        )


    elif candle == "weekly":

        monday = (
            now.normalize()
            -
            pd.Timedelta(
                days=now.weekday()
            )
        )

        key = monday


    elif candle == "hourly":

        key = now.floor("h")


    else:

        raise ValueError(
            candle
        )


    # --------------------------------------------------------
    # Find current candle
    # --------------------------------------------------------

    if candle == "monthly":

        mask = (
            (out["timestamp"].dt.year == key.year)
            &
            (out["timestamp"].dt.month == key.month)
        )


    elif candle == "weekly":

        mask = (
            out["timestamp"].dt.to_period("W-MON")
            ==
            key.to_period("W-MON")
        )


    else:

        mask = (
            out["timestamp"].dt.floor("h")
            ==
            key
        )


    # --------------------------------------------------------
    # Replace existing candle close
    # --------------------------------------------------------

    if mask.any():

        idx = out.index[
            mask
        ][-1]

        out.loc[
            idx,
            "close"
        ] = ltp


    # --------------------------------------------------------
    # Add current candle
    # --------------------------------------------------------

    else:

        row = {

            "timestamp":
                key,

            "open":
                ltp,

            "high":
                ltp,

            "low":
                ltp,

            "close":
                ltp,

            "volume":
                np.nan,

            "oi":
                np.nan
        }


        out = pd.concat(
            [
                out,
                pd.DataFrame([row])
            ],
            ignore_index=True
        )


    return (
        out
        .sort_values("timestamp")
        .drop_duplicates(
            "timestamp",
            keep="last"
        )
        .reset_index(drop=True)
    )


# ============================================================
# LOAD STOCK MASTER
# ============================================================

def load_master():

    df = pd.read_excel(
        MASTER_FILE,
        sheet_name=MASTER_SHEET
    )


    required = {
        "Symbol",
        "ISIN Code"
    }


    missing = (
        required -
        set(df.columns)
    )


    if missing:

        raise RuntimeError(
            "Missing required columns "
            f"in stock master: "
            f"{sorted(missing)}"
        )


    df = df.copy()


    df["Symbol"] = (
        df["Symbol"]
        .astype(str)
        .str.strip()
        .str.upper()
    )


    df["ISIN Code"] = (
        df["ISIN Code"]
        .astype(str)
        .str.strip()
        .str.upper()
    )


    df["instrument_key"] = (
        "NSE_EQ|"
        +
        df["ISIN Code"]
    )


    # --------------------------------------------------------
    # Only EQ stocks
    # --------------------------------------------------------

    df = df[
        df["Series"]
        .astype(str)
        .str.upper()
        .eq("EQ")
    ].copy()


    # One row per ISIN
    df = (
        df
        .drop_duplicates(
            subset=["ISIN Code"]
        )
        .reset_index(drop=True)
    )


    return df


# ============================================================
# EMPTY CROSS HISTORY
# ============================================================

def empty_cross_history():

    return pd.DataFrame(
        columns=[

            "Stock",
            "Company Name",
            "ISIN Code",
            "Instrument Key",

            "Cross Date",
            "Cross Price",

            "Previous Monthly RSI",
            "Cross Monthly RSI",

            "Detected On"
        ]
    )


# ============================================================
# EMPTY TOUCH HISTORY
# ============================================================

def empty_touch_history():

    return pd.DataFrame(
        columns=[

            "Stock",
            "Cross Date",

            "Touch #",
            "Touch Date",
            "Touch Time",

            "Hourly RSI",
            "Touch LTP"
        ]
    )


# ============================================================
# LOAD CSV OR EMPTY
# ============================================================

def load_csv_or_empty(
    path,
    empty_factory
):

    if path.exists():

        try:

            return pd.read_csv(
                path
            )

        except Exception:

            pass


    return empty_factory()


# ============================================================
# SAVE CSV
# ============================================================

def save_csv(
    df,
    path
):

    df.to_csv(
        path,
        index=False
    )


# ============================================================
# DETECT NEW MONTHLY CROSS
# ============================================================

def detect_new_monthly_cross(
    row
):

    """
    Detect only CONFIRMED completed monthly crosses.

    Current month is NOT used to create a permanent
    historical cross.
    """

    key = row[
        "instrument_key"
    ]


    end = datetime.now().date()


    start = (
        end -
        timedelta(
            days=365 * 3
        )
    )


    try:

        monthly = fetch_historical(
            key,
            "months",
            1,
            start,
            end
        )


        if len(monthly) < (
            RSI_PERIOD + 2
        ):

            return None


        # ----------------------------------------------------
        # Determine if last candle is current month
        # ----------------------------------------------------

        now = pd.Timestamp.now(
            tz="Asia/Kolkata"
        )


        latest = monthly.iloc[-1]


        latest_is_current = (

            latest["timestamp"].year
            ==
            now.year

            and

            latest["timestamp"].month
            ==
            now.month
        )


        if latest_is_current:

            completed = (
                monthly
                .iloc[:-1]
                .copy()
            )

        else:

            completed = (
                monthly
                .copy()
            )


        if len(completed) < (
            RSI_PERIOD + 2
        ):

            return None


        completed["rsi"] = (
            rsi_wilder(
                completed["close"],
                RSI_PERIOD
            )
        )


        prev = completed.iloc[-2]

        curr = completed.iloc[-1]


        # ----------------------------------------------------
        # Confirmed cross
        # ----------------------------------------------------

        if (

            pd.notna(prev["rsi"])

            and

            pd.notna(curr["rsi"])

            and

            float(prev["rsi"])
            <=
            MONTHLY_THRESHOLD

            and

            float(curr["rsi"])
            >
            MONTHLY_THRESHOLD

        ):

            return {

                "Stock":
                    row["Symbol"],

                "Company Name":
                    row.get(
                        "Company Name",
                        ""
                    ),

                "ISIN Code":
                    row["ISIN Code"],

                "Instrument Key":
                    key,

                "Cross Date":
                    pd.Timestamp(
                        curr["timestamp"]
                    ).date().isoformat(),

                "Cross Price":
                    float(
                        curr["close"]
                    ),

                "Previous Monthly RSI":
                    round(
                        float(
                            prev["rsi"]
                        ),
                        2
                    ),

                "Cross Monthly RSI":
                    round(
                        float(
                            curr["rsi"]
                        ),
                        2
                    ),

                "Detected On":
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
            }


    except Exception as e:

        print(
            f"[MONTHLY ERROR] "
            f"{row['Symbol']}: {e}"
        )


    return None


# ============================================================
# UPDATE CROSS HISTORY
# ============================================================

def update_cross_history(
    master,
    history
):

    existing_keys = set(
        history["ISIN Code"]
        .astype(str)
    ) if not history.empty else set()


    candidates = master[
        ~master["ISIN Code"]
        .isin(existing_keys)
    ].copy()


    print(
        "Monthly scan: "
        f"{len(candidates)} stocks "
        "not already in cross history."
    )


    found = []


    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {

            executor.submit(
                detect_new_monthly_cross,
                row
            ):
            row["Symbol"]

            for _, row
            in candidates.iterrows()
        }


        for future in as_completed(
            futures
        ):

            result = future.result()


            if result:

                found.append(
                    result
                )


                print(

                    f"[NEW CROSS] "
                    f"{result['Stock']} | "
                    f"{result['Cross Date']} | "
                    f"RSI "
                    f"{result['Cross Monthly RSI']} | "
                    f"Price "
                    f"{result['Cross Price']}"

                )


    if found:

        history = pd.concat(
            [
                history,
                pd.DataFrame(found)
            ],
            ignore_index=True
        )


        history = (
            history
            .drop_duplicates(
                subset=["ISIN Code"],
                keep="first"
            )
        )


    save_csv(
        history,
        CROSS_HISTORY_FILE
    )


    return history


# ============================================================
# CALCULATE MONTHLY / WEEKLY / HOURLY RSI
# ============================================================

def completed_and_live_rsi(
    key,
    ltp
):

    end = datetime.now().date()


    start = (
        end -
        timedelta(
            days=365 * 3
        )
    )


    # --------------------------------------------------------
    # Monthly
    # --------------------------------------------------------

    monthly = fetch_historical(
        key,
        "months",
        1,
        start,
        end
    )


    # --------------------------------------------------------
    # Weekly
    # --------------------------------------------------------

    weekly = fetch_historical(
        key,
        "weeks",
        1,
        start,
        end
    )


    # --------------------------------------------------------
    # Add live LTP
    # --------------------------------------------------------

    monthly_live = add_live_close(
        monthly,
        ltp,
        "monthly"
    )


    weekly_live = add_live_close(
        weekly,
        ltp,
        "weekly"
    )


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    monthly_live["rsi"] = (
        rsi_wilder(
            monthly_live["close"],
            RSI_PERIOD
        )
    )


    weekly_live["rsi"] = (
        rsi_wilder(
            weekly_live["close"],
            RSI_PERIOD
        )
    )


    m_rsi = (

        float(
            monthly_live.iloc[-1]["rsi"]
        )

        if pd.notna(
            monthly_live.iloc[-1]["rsi"]
        )

        else np.nan
    )


    w_rsi = (

        float(
            weekly_live.iloc[-1]["rsi"]
        )

        if pd.notna(
            weekly_live.iloc[-1]["rsi"]
        )

        else np.nan
    )


    # ========================================================
    # PREVIOUS COMPLETED MONTHLY RSI
    # ========================================================

    now = pd.Timestamp.now(
        tz="Asia/Kolkata"
    )


    completed_monthly = monthly[
        ~(
            (monthly["timestamp"].dt.year == now.year)
            &
            (monthly["timestamp"].dt.month == now.month)
        )
    ].copy()


    completed_monthly["rsi"] = (
        rsi_wilder(
            completed_monthly["close"],
            RSI_PERIOD
        )
    )


    if (
        len(completed_monthly)
        and
        pd.notna(
            completed_monthly.iloc[-1]["rsi"]
        )
    ):

        prev_m_rsi = float(
            completed_monthly.iloc[-1]["rsi"]
        )

    else:

        prev_m_rsi = np.nan


    # ========================================================
    # HOURLY
    # ========================================================

    hourly = fetch_historical(

        key,
        "hours",
        1,

        end -
        timedelta(
            days=80
        ),

        end
    )


    # --------------------------------------------------------
    # Add today's intraday hourly candles
    # --------------------------------------------------------

    try:

        intra = fetch_intraday_hours(
            key
        )


        if not intra.empty:

            hourly = pd.concat(
                [
                    hourly,
                    intra
                ],
                ignore_index=True
            )


            hourly["timestamp"] = (
                pd.to_datetime(
                    hourly["timestamp"],
                    utc=True
                )
                .dt
                .tz_convert(
                    "Asia/Kolkata"
                )
            )


            hourly = (
                hourly
                .sort_values(
                    "timestamp"
                )
                .drop_duplicates(
                    "timestamp",
                    keep="last"
                )
                .reset_index(
                    drop=True
                )
            )


    except Exception as e:

        print(
            f"[INTRADAY WARNING] "
            f"{key}: {e}"
        )


    # --------------------------------------------------------
    # Current live Hourly RSI
    # --------------------------------------------------------

    hourly_live = add_live_close(
        hourly,
        ltp,
        "hourly"
    )


    hourly_live["rsi"] = (
        rsi_wilder(
            hourly_live["close"],
            RSI_PERIOD
        )
    )


    h_rsi = (

        float(
            hourly_live.iloc[-1]["rsi"]
        )

        if pd.notna(
            hourly_live.iloc[-1]["rsi"]
        )

        else np.nan
    )


    return {

        "monthly":
            monthly_live,

        "weekly":
            weekly_live,

        "hourly":
            hourly_live,

        "monthly_rsi":
            m_rsi,

        "weekly_rsi":
            w_rsi,

        "hourly_rsi":
            h_rsi,

        "prev_monthly_rsi":
            prev_m_rsi
    }


# ============================================================
# HOURLY TOUCH EVENTS
# ============================================================

def touch_events(
    hourly_df,
    cross_date
):

    """
    A touch is one event where RSI <=30.

    Consecutive candles <=30 count as ONE event.

    A new event starts only after RSI recovers above 30.
    """

    if hourly_df.empty:

        return []


    df = hourly_df.copy()


    df["rsi"] = (
        rsi_wilder(
            df["close"],
            RSI_PERIOD
        )
    )


    cross_ts = pd.Timestamp(
        cross_date
    )


    # --------------------------------------------------------
    # Only candles after the historical cross
    # --------------------------------------------------------

    df = df[
        df["timestamp"].dt.date
        >=
        cross_ts.date()
    ].copy()


    events = []

    in_touch = False

    touch_no = 0


    for _, candle in df.iterrows():

        rsi = candle["rsi"]


        if pd.isna(rsi):

            continue


        below = (
            float(rsi)
            <=
            HOURLY_TOUCH_THRESHOLD
        )


        # ----------------------------------------------------
        # New touch
        # ----------------------------------------------------

        if below and not in_touch:

            touch_no += 1


            events.append({

                "Touch #":
                    touch_no,

                "Touch Date":
                    pd.Timestamp(
                        candle["timestamp"]
                    ).date().isoformat(),

                "Touch Time":
                    pd.Timestamp(
                        candle["timestamp"]
                    ).strftime("%H:%M"),

                "Hourly RSI":
                    round(
                        float(rsi),
                        2
                    )
            })


            in_touch = True


        # ----------------------------------------------------
        # Recovery above 30
        # ----------------------------------------------------

        elif not below:

            in_touch = False


    return events


# ============================================================
# STATUS FUNCTIONS
# ============================================================

def status_m(
    rsi
):

    if pd.isna(rsi):

        return "—"


    return (
        "🟢 >70"
        if rsi > 70
        else
        "🔴 <70"
    )


def status_w(
    rsi
):

    if pd.isna(rsi):

        return "—"


    return (
        "🟢 >50"
        if rsi > 50
        else
        "🔴 <50"
    )


def status_h(
    rsi
):

    if pd.isna(rsi):

        return "—"


    if rsi < 30:

        return "🔵 <30"


    if rsi <= 70:

        return "🟢 30–70"


    return "🔴 >70"


# ============================================================
# BUILD ACTIVE WATCHLIST
# ============================================================

def build_watchlist(
    master,
    cross_history
):

    """
    IMPORTANT:

    ACTIVE WATCHLIST is NOT based on cross_history.

    Every stock in the master is checked.

    Current Monthly RSI >70
        ->
    ACTIVE.

    Both types are included:

      1. HISTORICAL CROSS
      2. ALREADY ABOVE 70

    Weekly RSI and Hourly RSI are calculated
    for BOTH types.
    """

    if master.empty:

        return pd.DataFrame()


    # ========================================================
    # COMPLETE MASTER UNIVERSE
    # ========================================================

    master_rows = master.to_dict(
        "records"
    )


    master_keys = [
        row["instrument_key"]
        for row in master_rows
    ]


    # ========================================================
    # LTP FOR COMPLETE UNIVERSE
    # ========================================================

    ltps = fetch_ltp(
        master_keys
    )


    # ========================================================
    # HISTORICAL CROSS LOOKUP
    # ========================================================

    cross_by_isin = {}


    if not cross_history.empty:

        for _, cross in cross_history.iterrows():

            cross_by_isin[
                str(
                    cross["ISIN Code"]
                )
            ] = cross


    # ========================================================
    # LOAD TOUCH HISTORY
    # ========================================================

    touch_history = load_csv_or_empty(
        TOUCH_HISTORY_FILE,
        empty_touch_history
    )


    rows = []


    # ========================================================
    # SCAN COMPLETE UNIVERSE
    # ========================================================

    for row in master_rows:

        isin = str(
            row["ISIN Code"]
        )

        key = row[
            "instrument_key"
        ]

        symbol = row[
            "Symbol"
        ]


        try:

            # ------------------------------------------------
            # LTP
            # ------------------------------------------------

            ltp = ltps.get(
                key
            )


            if ltp is None:

                print(
                    f"[LTP MISSING] "
                    f"{symbol}"
                )

                continue


            ltp = float(
                ltp
            )


            # ------------------------------------------------
            # MONTHLY + WEEKLY + HOURLY RSI
            # ------------------------------------------------

            r = completed_and_live_rsi(
                key,
                ltp
            )


            m_rsi = r[
                "monthly_rsi"
            ]


            # ------------------------------------------------
            # ACTIVE CONDITION
            #
            # CURRENT Monthly RSI >70
            # ------------------------------------------------

            if (
                pd.isna(m_rsi)
                or
                float(m_rsi)
                <=
                MONTHLY_THRESHOLD
            ):

                continue


            # =================================================
            # HISTORICAL CROSS CHECK
            # =================================================

            cross = cross_by_isin.get(
                isin
            )


            has_cross = (
                cross is not None
            )


            if has_cross:

                entry_type = (
                    "HISTORICAL CROSS"
                )

                cross_date = str(
                    cross["Cross Date"]
                )

                cross_price = float(
                    cross["Cross Price"]
                )

            else:

                entry_type = (
                    "ALREADY ABOVE 70"
                )

                # NEVER INVENT HISTORICAL DATA
                cross_date = ""

                cross_price = np.nan


            # =================================================
            # HOURLY TOUCH MONITORING
            # =================================================

            hourly = r[
                "hourly"
            ]


            if has_cross:

                events = touch_events(
                    hourly,
                    cross_date
                )


                # ------------------------------------------------
                # Keep other stocks' history
                # ------------------------------------------------

                old = touch_history[
                    touch_history["Stock"]
                    !=
                    symbol
                ].copy()


                new_touch_rows = []


                for event in events:

                    new_touch_rows.append({

                        "Stock":
                            symbol,

                        "Cross Date":
                            cross_date,

                        **event,

                        "Touch LTP":
                            np.nan
                    })


                # ------------------------------------------------
                # Find LTP at touch time
                # ------------------------------------------------

                for touch_row in new_touch_rows:

                    timestamp = pd.Timestamp(

                        f"{touch_row['Touch Date']} "
                        f"{touch_row['Touch Time']}",

                        tz="Asia/Kolkata"
                    )


                    match = hourly.iloc[

                        (
                            hourly["timestamp"]
                            -
                            timestamp
                        )
                        .abs()
                        .argsort()[:1]

                    ]


                    if not match.empty:

                        touch_row[
                            "Touch LTP"
                        ] = float(
                            match.iloc[0]["close"]
                        )


                touch_history = pd.concat(

                    [
                        old,
                        pd.DataFrame(
                            new_touch_rows
                        )
                    ],

                    ignore_index=True
                )


                touch_history = (
                    touch_history
                    .drop_duplicates(
                        subset=[
                            "Stock",
                            "Cross Date",
                            "Touch #"
                        ],
                        keep="last"
                    )
                )


                stock_touch = touch_history[

                    touch_history["Stock"].eq(
                        symbol
                    )

                    &

                    touch_history[
                        "Cross Date"
                    ].eq(
                        cross_date
                    )

                ].sort_values(
                    "Touch #"
                )


            else:

                # No historical cross means we cannot
                # create historical touch events.
                stock_touch = pd.DataFrame(
                    columns=[
                        "Touch #",
                        "Touch Date",
                        "Touch Time",
                        "Touch LTP"
                    ]
                )


            # =================================================
            # GROWTH
            # =================================================

            if (

                has_cross

                and

                pd.notna(
                    cross_price
                )

                and

                cross_price > 0

            ):

                growth = (

                    (
                        ltp -
                        cross_price
                    )
                    /
                    cross_price

                ) * 100


                # ------------------------------------------------
                # Daily candles after cross
                # ------------------------------------------------

                daily = fetch_historical(

                    key,

                    "days",

                    1,

                    pd.Timestamp(
                        cross_date
                    ).date(),

                    datetime.now().date()
                )


                if not daily.empty:

                    max_price = float(
                        daily["high"].max()
                    )

                else:

                    max_price = ltp


                max_growth = (

                    (
                        max_price -
                        cross_price
                    )
                    /
                    cross_price

                ) * 100


                drawdown = (
                    ((ltp - max_price) / max_price) * 100
                    if max_price else np.nan
                        )


                days_since = (

                    datetime.now().date()
                    -
                    pd.Timestamp(
                        cross_date
                    ).date()

                ).days


            else:

                # No historical cross
                # therefore no historical growth.

                growth = np.nan

                max_growth = np.nan

                drawdown = np.nan

                days_since = np.nan


            # =================================================
            # TOUCH DATE STRING
            # =================================================

            touch_dates = ", ".join(

                f"{touch['Touch Date']} "
                f"{touch['Touch Time']}"

                for _, touch
                in stock_touch.iterrows()

            )


            # =================================================
            # ADD ACTIVE ROW
            # =================================================

            rows.append({

                "Stock":
                    symbol,

                "Company Name":
                    row.get(
                        "Company Name",
                        ""
                    ),

                "Entry Type":
                    entry_type,

                "Cross Date":
                    cross_date,

                "Cross Price":
                    (
                        round(
                            cross_price,
                            2
                        )
                        if has_cross
                        else np.nan
                    ),

                "LTP":
                    round(
                        ltp,
                        2
                    ),

                "Growth %":
                    (
                        round(
                            growth,
                            2
                        )
                        if pd.notna(
                            growth
                        )
                        else np.nan
                    ),

                "Max Growth %":
                    (
                        round(
                            max_growth,
                            2
                        )
                        if pd.notna(
                            max_growth
                        )
                        else np.nan
                    ),

                "Drawdown %":
                    (
                        round(
                            drawdown,
                            2
                        )
                        if pd.notna(
                            drawdown
                        )
                        else np.nan
                    ),

                # =============================================
                # RSI
                # =============================================

                "Monthly RSI":
                    round(
                        m_rsi,
                        2
                    ),

                "Weekly RSI":
                    (
                        round(
                            r["weekly_rsi"],
                            2
                        )
                        if pd.notna(
                            r["weekly_rsi"]
                        )
                        else np.nan
                    ),

                "Hourly RSI":
                    (
                        round(
                            r["hourly_rsi"],
                            2
                        )
                        if pd.notna(
                            r["hourly_rsi"]
                        )
                        else np.nan
                    ),

                "Prev Monthly RSI":
                    (
                        round(
                            r["prev_monthly_rsi"],
                            2
                        )
                        if pd.notna(
                            r["prev_monthly_rsi"]
                        )
                        else np.nan
                    ),

                "Days Since Cross":
                    days_since,

                # =============================================
                # Hourly touch
                # =============================================

                "H-RSI Touch ≤30 Count":
                    int(
                        len(
                            stock_touch
                        )
                    ),

                "H-RSI Touch Dates":
                    touch_dates,

                # =============================================
                # Status
                # =============================================

                "M-RSI Status":
                    status_m(
                        m_rsi
                    ),

                "W-RSI Status":
                    status_w(
                        r["weekly_rsi"]
                    ),

                "H-RSI Status":
                    status_h(
                        r["hourly_rsi"]
                    ),

                # =============================================
                # Instrument
                # =============================================

                "ISIN Code":
                    isin,

                "Instrument Key":
                    key
            })


            # =================================================
            # LOG
            # =================================================

            print(

                f"[ACTIVE] "
                f"{symbol} | "

                f"{entry_type} | "

                f"M={m_rsi:.2f} | "

                f"W={r['weekly_rsi']:.2f} | "

                f"H={r['hourly_rsi']:.2f} | "

                f"Touches="
                f"{len(stock_touch)}"

            )


        except Exception as e:

            print(

                f"[WATCH ERROR] "
                f"{symbol}: "
                f"{e}"

            )


    # ========================================================
    # SAVE TOUCH HISTORY
    # ========================================================

    save_csv(
        touch_history,
        TOUCH_HISTORY_FILE
    )


    # ========================================================
    # WATCHLIST COLUMNS
    # ========================================================

    columns = [

        "Stock",
        "Company Name",

        "Entry Type",

        "Cross Date",
        "Cross Price",

        "LTP",

        "Growth %",
        "Max Growth %",
        "Drawdown %",

        "Monthly RSI",
        "Weekly RSI",
        "Hourly RSI",

        "Prev Monthly RSI",

        "Days Since Cross",

        "H-RSI Touch ≤30 Count",
        "H-RSI Touch Dates",

        "M-RSI Status",
        "W-RSI Status",
        "H-RSI Status",

        "ISIN Code",
        "Instrument Key"
    ]


    result = pd.DataFrame(
        rows,
        columns=columns
    )


    # ========================================================
    # NO ACTIVE STOCKS
    # ========================================================

    if result.empty:

        save_csv(
            result,
            WATCHLIST_CSV_FILE
        )

        return result


    # ========================================================
    # SORT
    #
    # Highest Monthly RSI first.
    # Growth is secondary.
    # ========================================================

    result = result.sort_values(

        [
            "Monthly RSI",
            "Growth %"
        ],

        ascending=[
            False,
            False
        ],

        na_position="last"

    ).reset_index(
        drop=True
    )


    # ========================================================
    # SAVE DASHBOARD CSV
    # ========================================================

    save_csv(
        result,
        WATCHLIST_CSV_FILE
    )


    print(
        "\nActive Monthly RSI >70 stocks: "
        f"{len(result)}"
    )


    return result


# ============================================================
# WRITE EXCEL
# ============================================================

def write_excel(
    watchlist,
    cross_history,
    touch_history
):

    with pd.ExcelWriter(
        WATCHLIST_FILE,
        engine="openpyxl"
    ) as writer:

        watchlist.to_excel(
            writer,
            sheet_name="Watchlist",
            index=False
        )


        cross_history.to_excel(
            writer,
            sheet_name="Cross History",
            index=False
        )


        touch_history.to_excel(
            writer,
            sheet_name="Hourly Touch History",
            index=False
        )


        # ----------------------------------------------------
        # Basic formatting
        # ----------------------------------------------------

        workbook = writer.book


        for worksheet in workbook.worksheets:

            worksheet.freeze_panes = "A2"

            worksheet.auto_filter.ref = (
                worksheet.dimensions
            )


            for column in worksheet.columns:

                max_len = 0

                letter = (
                    column[0]
                    .column_letter
                )


                for cell in column[:1000]:

                    value = (
                        ""
                        if cell.value is None
                        else str(cell.value)
                    )


                    max_len = max(
                        max_len,
                        len(value)
                    )


                worksheet.column_dimensions[
                    letter
                ].width = min(
                    max(
                        max_len + 2,
                        10
                    ),
                    40
                )


# ============================================================
# RUN LOG
# ============================================================

def append_run_log(
    status,
    message
):

    row = pd.DataFrame([{

        "Run Time":
            datetime.now().isoformat(
                timespec="seconds"
            ),

        "Status":
            status,

        "Message":
            message
    }])


    if RUN_LOG_FILE.exists():

        old = pd.read_csv(
            RUN_LOG_FILE
        )


        row = pd.concat(
            [
                old,
                row
            ],
            ignore_index=True
        )


    row.to_csv(
        RUN_LOG_FILE,
        index=False
    )


# ============================================================
# MAIN
# ============================================================

def main():

    die_if_no_token()


    print(
        "=" * 70
    )

    print(
        "MONTHLY RSI >70 ACTIVE WATCHLIST"
    )

    print(
        "=" * 70
    )


    # ========================================================
    # LOAD MASTER
    # ========================================================

    master = load_master()


    print(
        f"Stock master rows: "
        f"{len(master)}"
    )


    # ========================================================
    # LOAD CROSS HISTORY
    # ========================================================

    cross_history = load_csv_or_empty(

        CROSS_HISTORY_FILE,

        empty_cross_history
    )


    # ========================================================
    # UPDATE HISTORICAL CROSSES
    # ========================================================

    cross_history = update_cross_history(

        master,

        cross_history
    )


    print(

        f"Total crossed stocks stored: "
        f"{len(cross_history)}"

    )


    # ========================================================
    # BUILD ACTIVE WATCHLIST
    # ========================================================

    watchlist = build_watchlist(

        master,

        cross_history
    )


    # ========================================================
    # LOAD TOUCH HISTORY FOR EXCEL
    # ========================================================

    touch_history = load_csv_or_empty(

        TOUCH_HISTORY_FILE,

        empty_touch_history
    )


    # ========================================================
    # NO ACTIVE STOCKS
    # ========================================================

    if watchlist.empty:

        print(
            "No active Monthly RSI >70 "
            "stocks found."
        )


        empty_columns = [

            "Stock",
            "Company Name",
            "Entry Type",

            "Cross Date",
            "Cross Price",

            "LTP",

            "Growth %",
            "Max Growth %",
            "Drawdown %",

            "Monthly RSI",
            "Weekly RSI",
            "Hourly RSI",

            "Prev Monthly RSI",

            "Days Since Cross",

            "H-RSI Touch ≤30 Count",
            "H-RSI Touch Dates",

            "M-RSI Status",
            "W-RSI Status",
            "H-RSI Status",

            "ISIN Code",
            "Instrument Key"
        ]


        empty_watchlist = pd.DataFrame(
            columns=empty_columns
        )


        save_csv(
            empty_watchlist,
            WATCHLIST_CSV_FILE
        )


        write_excel(

            empty_watchlist,

            cross_history,

            touch_history

        )


        append_run_log(

            "SUCCESS",

            "No active Monthly RSI >70 stocks."

        )


        return


    # ========================================================
    # WRITE EXCEL
    # ========================================================

    write_excel(

        watchlist,

        cross_history,

        touch_history

    )


    # ========================================================
    # DISPLAY
    # ========================================================

    print(
        "\n"
        + "=" * 70
    )

    print(
        "ACTIVE WATCHLIST"
    )

    print(
        "=" * 70
    )


    display_cols = [

        "Stock",
        "Entry Type",

        "Cross Date",
        "Cross Price",

        "LTP",

        "Growth %",

        "Monthly RSI",
        "Weekly RSI",
        "Hourly RSI",

        "Prev Monthly RSI",

        "Days Since Cross",

        "H-RSI Touch ≤30 Count",

        "M-RSI Status",
        "W-RSI Status",
        "H-RSI Status"
    ]


    print(

        watchlist[
            display_cols
        ].to_string(
            index=False
        )

    )


    # ========================================================
    # SAVED FILES
    # ========================================================

    print(
        "\nSaved:"
    )


    print(
        f"  {WATCHLIST_FILE}"
    )


    print(
        f"  {WATCHLIST_CSV_FILE}"
    )


    print(
        f"  {CROSS_HISTORY_FILE}"
    )


    print(
        f"  {TOUCH_HISTORY_FILE}"
    )


    append_run_log(

        "SUCCESS",

        f"Updated "
        f"{len(watchlist)} "
        f"active Monthly RSI >70 stocks."

    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()