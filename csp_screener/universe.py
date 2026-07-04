"""
Hardcoded universe of US-listed optionable stocks suitable for cash-secured puts
on a small account.

Selection criteria (applied manually):
  - US-listed common stock or popular ETF
  - Price typically $5-25 over the last 12 months
  - Avg daily volume > 1M shares (filter still rechecks at runtime)
  - Weekly options available (skip monthly-only names)
  - Not in active bankruptcy / not a SPAC / not a recent IPO (<6 mo)

This list is the SOURCE of the screener. Better to be short and good than long
and noisy. Reviewed quarterly — not by the bot, by you.
"""

# Liquid mid-low-priced US stocks in the $5-25 band as of late 2025.
# Source: manually curated from S&P/Russell membership + IBKR most-active lists.
UNIVERSE: list[str] = [
    # ---- Financials / Banks (well-bid options, decent IV) ----
    "F",        # Ford Motor
    "BAC",      # Bank of America
    "WFC",      # Wells Fargo
    "T",        # AT&T
    "VZ",       # Verizon
    "KEY",      # KeyCorp
    "RF",       # Regions Financial
    "CFG",      # Citizens Financial
    "HBAN",     # Huntington Bancshares
    "FITB",     # Fifth Third
    "MTB",      # M&T Bank
    "SCHW",     # Schwab (might exceed but watch)
    "SOFI",     # SoFi Technologies — very liquid options
    "PYPL",     # PayPal (sometimes in range)

    # ---- Energy / Oil & Gas ----
    "WMB",      # Williams Companies
    "KMI",      # Kinder Morgan
    "FCX",      # Freeport-McMoRan
    "DVN",      # Devon Energy
    "ET",       # Energy Transfer
    "EPD",      # Enterprise Products Partners
    "MPLX",     # MPLX LP

    # ---- Tech / Internet (the cheap end) ----
    "PLTR",     # Palantir — high IV, very liquid options
    "SNAP",     # Snap
    "PINS",     # Pinterest
    "RBLX",     # Roblox
    "U",        # Unity Software
    "FUBO",     # FuboTV
    "GPRO",     # GoPro
    "BBAI",     # BigBear.ai
    "SOUN",     # SoundHound AI
    "RIOT",     # Riot Platforms
    "MARA",     # Marathon Digital
    "DKNG",     # DraftKings (might be over $25 — runtime filter handles)
    "RKT",      # Rocket Companies
    "OPEN",     # Opendoor

    # ---- Healthcare / Biotech (under-$25 large caps) ----
    "PFE",      # Pfizer
    "BMY",      # Bristol-Myers Squibb (may exceed)
    "MRNA",     # Moderna (usually in range)
    "GME",      # GameStop (meme, very high IV — caution)
    "CGC",      # Canopy Growth
    "TLRY",     # Tilray
    "SNDL",     # SNDL Inc
    "NIO",      # NIO Inc (Chinese EV — caution)
    "CHPT",     # ChargePoint
    "BLNK",     # Blink Charging

    # ---- Industrials / Materials ----
    "GE",       # GE (usually over but watch)
    "AA",       # Alcoa
    "CLF",      # Cleveland-Cliffs
    "VALE",     # Vale SA
    "RIG",      # Transocean
    "BTU",      # Peabody Energy

    # ---- Consumer / Retail ----
    "M",        # Macy's
    "KSS",      # Kohl's
    "BBY",      # Best Buy (may exceed)
    "CVS",      # CVS Health (may exceed)
    "SHO",      # Sunstone Hotel
    "WEN",      # Wendy's
    "JACK",     # Jack in the Box

    # ---- Transportation ----
    "DAL",      # Delta Air Lines (sometimes in range)
    "AAL",      # American Airlines
    "UAL",      # United Airlines (may exceed)
    "JBLU",     # JetBlue
    "LUV",      # Southwest Airlines
    "ALK",      # Alaska Air
    "CCL",      # Carnival Cruise
    "NCLH",     # Norwegian Cruise
    "RCL",      # Royal Caribbean (may exceed)

    # ---- Real Estate / REITs (small) ----
    "OPI",      # Office Properties Income Trust
    "AGNC",     # AGNC Investment

    # ---- ETFs (liquid, low-priced) ----
    "XLE",      # Energy Select Sector
    "XLF",      # Financial Select Sector (often >$25 — check)
    "EEM",      # Emerging Markets
    "GDX",      # Gold Miners
    "GDXJ",     # Junior Gold Miners
    "SLV",      # Silver Trust
    "USO",      # US Oil Fund
    "TLT",      # 20+ Year Treasuries (often >$25)
    "HYG",      # High Yield Corp Bond
    "EWZ",      # Brazil
    "FXI",      # China Large-Cap

    # ---- Crypto-exposed / Speculative (caution: high IV) ----
    "COIN",     # Coinbase (usually >$25)
    "HOOD",     # Robinhood (sometimes in range)
    "AFRM",     # Affirm (sometimes in range)
    "XYZ",      # Block (formerly SQ — renamed ticker)

    # ---- Misc liquid mid-caps ----
    "AMC",      # AMC Entertainment (meme caution)
    "BB",       # BlackBerry
    "NOK",      # Nokia
    "ERIC",     # Ericsson
    "GRAB",     # Grab Holdings
    "LCID",     # Lucid Motors
    "RIVN",     # Rivian (sometimes in range)
    "GM",       # General Motors (often >$25)
    "WBD",      # Warner Bros Discovery
]

# Deduplicate while preserving order
seen = set()
UNIVERSE = [t for t in UNIVERSE if not (t in seen or seen.add(t))]


def get_universe() -> list[str]:
    """Return the canonical universe list."""
    return list(UNIVERSE)


def universe_size() -> int:
    return len(UNIVERSE)
