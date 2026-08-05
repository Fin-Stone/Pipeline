"""The rules that ship with the product.

Layer one of the bootstrap in §3.1a. Two things it deliberately is not:

**Not scraped POI data.** Matching a statement descriptor like
`MALL EXAMPLE DUMPLING` to an OpenStreetMap entry is its own problem, and a
lookup at classify time would put a network call between the operator and their
own ledger — which §5.2 forbids in a self-hosted install. A curated list
versioned in git does the same job, ships with the product, and can be read.

**Not a claim to be right.** These are the well-known chains, and the tail of
any real ledger is local: this corpus contains `TS/Example Diner` and
`BUS/MRT 100200300`, neither of which any general dataset would hold. The seed
exists to take the obvious cases off the table so that judgement — human or
model — is spent on the rest.

Patterns are matched against `counterparty_norm`, case-insensitively, and the
narrower one wins on its own (see `categories.Rule.specificity`), which is what
lets an exception sit beside the general case without a weight.
"""

from __future__ import annotations

#: (pattern, category, note). Ordered by category for reading, not by priority
#: — priority is specificity and weight, decided at match time.
SEED_RULES: tuple[tuple[str, str, str], ...] = (
    # --- Grocery -----------------------------------------------------------
    (r"\b(?:ntuc|fairprice|finest)\b", "Grocery", "NTUC FairPrice"),
    (r"\bsheng\s*siong\b", "Grocery", ""),
    (r"\bcold\s*storage\b", "Grocery", ""),
    (r"\b(?:giant|prime\s+super|hao\s*mart)\b", "Grocery", ""),
    (r"\bdon\s*don\s*donki|\bdonki\b", "Grocery", ""),
    (r"\b(?:7-?eleven|cheers)\b", "Grocery", "Convenience"),

    # --- Dining ------------------------------------------------------------
    # Sits above the chains it belongs to: a furniture shop's restaurant is
    # Dining, and this corpus contains exactly that case.
    (r"\bikea[\s-]*restaurant\b", "Dining", "The IKEA-RESTAURANT case"),
    (r"\b(?:mcdonald|kfc|burger\s*king|subway|popeyes|texas\s*chicken)\b", "Dining", ""),
    (r"\b(?:starbucks|coffee\s*bean|toast\s*box|ya\s*kun|kopitiam|koufu|food\s*republic)\b",
     "Dining", ""),
    (r"\b(?:pizza\s*hut|domino|swensen|sushi|ramen|shokudo|dian\s*xiao\s*er)\b", "Dining", ""),
    (r"\b(?:foodpanda|deliveroo|grabfood)\b", "Dining", "Delivery"),
    (r"\b(?:restaurant|eating\s*house|hawker|cafe|bistro|bakery)\b", "Dining", "Generic"),

    # --- Transport ---------------------------------------------------------
    (r"\b(?:bus/mrt|smrt|sbs\s*transit|transitlink)\b", "Transport", ""),
    (r"\b(?:comfort|citycab|trans-?cab|premier\s*taxi)\b", "Transport", ""),
    (r"\bgrab\b(?!food)", "Transport", "Rides; GrabFood is Dining"),
    (r"\b(?:gojek|tada|zig)\b", "Transport", ""),
    (r"\b(?:esso|shell|caltex|spc|sinopec)\b", "Transport", "Fuel"),
    (r"\b(?:parking|carpark|wilson\s*parking|season\s*park|hdb\s*park)\b", "Transport", ""),
    (r"\b(?:ez-?link|cashcard|flashpay)\b", "Transport", "Transit stored value"),

    # --- Bills and utilities ----------------------------------------------
    (r"\b(?:singtel|starhub|m1\b|simba|circles\.?life|myrepublic)\b",
     "Bills and utilities", "Telco; mobile plans belong here"),
    (r"\b(?:sp\s*services|sp\s*group|city\s*gas|pub\b|senoko|geneco|keppel\s*electric)\b",
     "Bills and utilities", ""),
    (r"\b(?:town\s*council|conservancy|s&cc)\b", "Bills and utilities", ""),

    # --- Insurance ---------------------------------------------------------
    (r"\b(?:aia|prudential|great\s*eastern|singapore\s*life|singlife|manulife)\b",
     "Insurance", ""),
    (r"\b(?:ntuc\s*income|income\s*insurance|etiqa|fwd\b|msig|aviva)\b", "Insurance", ""),
    (r"\binsurance\b", "Insurance", "Generic"),

    # --- Healthcare --------------------------------------------------------
    (r"\b(?:clinic|polyclinic|hospital|medical|dental|dentist|specialist)\b", "Healthcare", ""),
    (r"\b(?:watsons|guardian|unity\s*pharmacy|pharmacy)\b", "Healthcare", ""),
    (r"\b(?:raffles\s*medical|parkway|mount\s*elizabeth|gleneagles|healthway)\b",
     "Healthcare", ""),

    # --- Wellness ----------------------------------------------------------
    (r"\b(?:massage|spa\b|nail|salon|barber|hair)\b", "Wellness", ""),
    (r"\b(?:anytime\s*fitness|fitness\s*first|virgin\s*active|gymm?\b|pure\s*fitness)\b",
     "Wellness", ""),

    # --- Recreation --------------------------------------------------------
    (r"\b(?:golden\s*village|cathay\s*cine|shaw\s*(?:theatre|lido)|filmgarde)\b",
     "Recreation", ""),
    (r"\b(?:netflix|spotify|disney\s*plus|hbo|viu\b|crunchyroll)\b", "Recreation", "Streaming"),
    (r"\b(?:steam\s*games|playstation|nintendo|xbox)\b", "Recreation", ""),
    (r"\b(?:zoo|bird\s*paradise|s\.?e\.?a\.?\s*aquarium|universal\s*studios|gardens\s*by)\b",
     "Recreation", ""),

    # --- Travel ------------------------------------------------------------
    (r"\b(?:singapore\s*airlines|scoot|jetstar|air\s*asia|emirates|cathay\s*pacific)\b",
     "Travel", ""),
    (r"\b(?:agoda|booking\.?com|expedia|airbnb|trip\.?com|klook)\b", "Travel", ""),
    (r"\b(?:hotel|resort|hostel)\b", "Travel", "Generic"),

    # --- Furnishing --------------------------------------------------------
    (r"\bikea\b", "Furnishing", "Beaten by the restaurant rule above"),
    (r"\b(?:courts|harvey\s*norman|fortytwo|castlery|hipvan|taobao\s*furniture)\b",
     "Furnishing", ""),
    (r"\b(?:renovation|contractor|carpentry|plumbing|electrician|tiling)\b", "Furnishing", ""),
    (r"\b(?:hardware|home-?fix|selffix|horme)\b", "Furnishing", ""),

    # --- Electronics -------------------------------------------------------
    (r"\b(?:challenger|best\s*denki|gain\s*city|audio\s*house)\b", "Electronics", ""),
    (r"\b(?:apple\s*store|samsung|xiaomi|aftershock|dell|lenovo|asus)\b", "Electronics", ""),
    (r"\b(?:sim\s*lim|funan)\b", "Electronics", ""),

    # --- Fashion -----------------------------------------------------------
    (r"\b(?:uniqlo|zara|h\s*&\s*m|cotton\s*on|muji|decathlon)\b", "Fashion", ""),
    (r"\b(?:nike|adidas|new\s*balance|skechers|charles\s*&\s*keith|pedro)\b", "Fashion", ""),

    # --- Business services -------------------------------------------------
    (r"\b(?:acra|iras\b|cpf\b)\b", "Business services", "Statutory"),
    (r"\b(?:accounting|audit|legal|law\s*corp|consultanc)\b", "Business services", ""),
)


def seed_rules():
    """The bundled rules, as `categories.Rule` objects."""
    from .categories import Rule

    return [
        Rule(pattern=pattern, category=category, note=note or "seed")
        for pattern, category, note in SEED_RULES
    ]
