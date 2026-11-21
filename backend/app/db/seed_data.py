"""Initial reference data: data sources and the supported instruments.

Seeding is idempotent and never overwrites values you change later
(`active`, `last_success_at`, `created_at` are only set on first insert).

NOTE: `yfinance_symbol` values are the commonly used Yahoo Finance tickers.
They are checked against real downloads in Phase 4.
"""

# Canonical symbol rule: UPPER CASE, no spaces, no exchange suffix.
# Display names live in `name`; provider-specific tickers in `yfinance_symbol`.

SOURCES = [
    {
        "source_id": "nse_bhavcopy",
        "name": "NSE India - Bhavcopy and market reports",
        "url": "https://www.nseindia.com/market-data",
        "data_class": "historical",
        "requires_auth": False,
        "license_notes": "Official end-of-day files. Check NSE's current terms of use before bulk or "
                         "automated downloads; the site restricts non-browser traffic. Do not bypass restrictions.",
    },
    {
        "source_id": "bse_bhavcopy",
        "name": "BSE India - Bhavcopy and market data",
        "url": "https://www.bseindia.com/",
        "data_class": "historical",
        "requires_auth": False,
        "license_notes": "Official end-of-day files. Check BSE's current terms of use before automated downloads.",
    },
    {
        "source_id": "kaggle",
        "name": "Kaggle community datasets",
        "url": "https://www.kaggle.com/datasets",
        "data_class": "historical",
        "requires_auth": True,
        "license_notes": "Free account and API token required. Each dataset has its own license: "
                         "record the license per dataset at import time.",
    },
    {
        "source_id": "yfinance",
        "name": "Yahoo Finance via yfinance",
        "url": "https://finance.yahoo.com/",
        "data_class": "delayed",
        "requires_auth": False,
        "license_notes": "Unofficial, for personal/research use. Not a licensed exchange feed. "
                         "Intraday history is limited (about 7 days for 1m, 60 days for 5m-30m, 730 days for 1h).",
    },
    {
        "source_id": "broker_api",
        "name": "Broker API (live and historical candles)",
        "url": "",
        "data_class": "real_time",
        "requires_auth": True,
        "license_notes": "Requires a broker account and API permissions. Credentials only in .env.",
    },
    {
        "source_id": "rbi",
        "name": "Reserve Bank of India",
        "url": "https://www.rbi.org.in/",
        "data_class": "historical",
        "requires_auth": False,
        "license_notes": "Official macro and monetary data. Cite the publication for every series.",
    },
    {
        "source_id": "mospi",
        "name": "Ministry of Statistics and Programme Implementation",
        "url": "https://www.mospi.gov.in/",
        "data_class": "historical",
        "requires_auth": False,
        "license_notes": "Official statistics (CPI, GDP, IIP). Cite the publication for every series.",
    },
    {
        "source_id": "manual_upload",
        "name": "Manual CSV / Excel upload",
        "url": "",
        "data_class": "historical",
        "requires_auth": False,
        "license_notes": "Files uploaded by the user; origin and license recorded at upload time.",
    },
    {
        "source_id": "synthetic_benchmark",
        "name": "Synthetic benchmark generator",
        "url": "",
        "data_class": "synthetic",
        "requires_auth": False,
        "license_notes": "Randomly generated data used ONLY for performance tests. Never real market data.",
    },
]

# (symbol, name, sector, industry)
_EQUITIES = [
    ("RELIANCE", "Reliance Industries Ltd", "Oil, Gas & Consumable Fuels", "Refineries & Marketing"),
    ("TCS", "Tata Consultancy Services Ltd", "Information Technology", "IT Services"),
    ("HDFCBANK", "HDFC Bank Ltd", "Financial Services", "Private Sector Bank"),
    ("ICICIBANK", "ICICI Bank Ltd", "Financial Services", "Private Sector Bank"),
    ("INFY", "Infosys Ltd", "Information Technology", "IT Services"),
    ("SBIN", "State Bank of India", "Financial Services", "Public Sector Bank"),
    ("ITC", "ITC Ltd", "Fast Moving Consumer Goods", "Diversified FMCG"),
    ("BHARTIARTL", "Bharti Airtel Ltd", "Telecommunication", "Telecom Services"),
    ("LT", "Larsen & Toubro Ltd", "Construction", "Civil Construction"),
    ("AXISBANK", "Axis Bank Ltd", "Financial Services", "Private Sector Bank"),
]

# (symbol, display name, exchange, yfinance ticker)
_INDICES = [
    ("NIFTY_50", "NIFTY 50", "NSE", "^NSEI"),
    ("NIFTY_BANK", "NIFTY BANK", "NSE", "^NSEBANK"),
    ("NIFTY_IT", "NIFTY IT", "NSE", "^CNXIT"),
    ("NIFTY_FIN_SERVICE", "NIFTY FINANCIAL SERVICES", "NSE", "NIFTY_FIN_SERVICE.NS"),
    ("SENSEX", "S&P BSE SENSEX", "BSE", "^BSESN"),
    ("BSE_500", "S&P BSE 500", "BSE", "BSE-500.BO"),
]

INSTRUMENTS = [
    {
        "symbol": sym,
        "exchange": "NSE",
        "name": name,
        "instrument_type": "equity",
        "sector": sector,
        "industry": industry,
        "yfinance_symbol": f"{sym}.NS",
        "currency": "INR",
    }
    for sym, name, sector, industry in _EQUITIES
] + [
    {
        "symbol": sym,
        "exchange": exch,
        "name": name,
        "instrument_type": "index",
        "yfinance_symbol": yf,
        "currency": "INR",
    }
    for sym, name, exch, yf in _INDICES
]
