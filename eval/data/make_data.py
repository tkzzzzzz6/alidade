"""Generate the extra evaluation tables in eval/data/ (deterministic).

    python eval/data/make_data.py

* messy_sales.csv   — synthetic orders with the usual dirt: three date formats,
                      amounts as "$1,234.50" strings, blanks / "N/A", region
                      names in mixed case with stray spaces, duplicated orders.
                      The data is built so a naive analysis gets a different
                      answer than a cleaned one.
* stock_info.csv    — symbol → company / sector (for a join with stock_history).
* country_continent.csv — country → continent (for a join with global_energy;
                      the key column is named differently on purpose).

Ground truths for the questions are printed at the end (and written to
eval/questions.yaml by hand).
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent


def make_messy_sales() -> pd.DataFrame:
    rng = random.Random(20261010)
    regions = {
        # true region → raw spellings and how many orders it gets
        "North": (["North", "north", " NORTH "], 150),
        "East": (["East"], 135),
        "West": (["West", "west"], 120),
        "South": (["South", "south"], 110),
    }
    month_weight = {1: 1.0, 2: 0.9, 3: 1.0, 4: 1.0, 5: 1.1, 6: 1.0,
                    7: 1.0, 8: 0.95, 9: 1.0, 10: 1.05, 11: 1.0, 12: 1.0}
    rows, oid = [], 10000
    for region, (spellings, n) in regions.items():
        for _ in range(n):
            oid += 1
            month = rng.choices(list(month_weight), weights=list(month_weight.values()))[0]
            day = rng.randint(1, 28)
            d = date(2024, month, day)
            amount = round(rng.uniform(80, 900), 2)
            if month == 3:          # March is the real peak …
                amount = round(amount * 1.6, 2)
            rows.append({"order_id": f"A{oid}", "date": d, "region": rng.choice(spellings), "amount": amount})

    # … but most March orders use the "Mar 5, 2024" spelling, so code that only
    # parses ISO dates loses them and finds a different peak month.
    for r in rows:
        d = r["date"]
        if d.month == 3:
            fmt = rng.choices(["text", "iso", "slash"], weights=[0.75, 0.15, 0.10])[0]
        else:
            fmt = rng.choices(["iso", "slash", "text"], weights=[0.7, 0.2, 0.1])[0]
        r["order_date"] = (d.strftime("%Y-%m-%d") if fmt == "iso"
                           else d.strftime("%Y/%m/%d") if fmt == "slash"
                           else d.strftime("%b %d, %Y").replace(" 0", " "))

    # Amounts as strings: "$1,234.50", some blanks and "N/A".
    for r in rows:
        roll = rng.random()
        if roll < 0.06:
            r["amount_str"] = ""
        elif roll < 0.09:
            r["amount_str"] = "N/A"
        else:
            r["amount_str"] = f"${r['amount']:,.2f}"

    # Duplicated orders: West rows are re-exported many times, so summing without
    # de-duplicating makes West look biggest.
    dupes = [dict(r) for r in rows if r["region"] in ("West", "west") and rng.random() < 0.55]
    dupes += [dict(r) for r in rng.sample(rows, 12)]
    all_rows = rows + dupes
    rng.shuffle(all_rows)
    return pd.DataFrame([{"order_id": r["order_id"], "order_date": r["order_date"], "region": r["region"],
                          "amount": r["amount_str"]} for r in all_rows])


STOCK_INFO = [
    ("AAPL", "Apple", "Information Technology"),
    ("MSFT", "Microsoft", "Information Technology"),
    ("NVDA", "NVIDIA", "Information Technology"),
    ("GOOGL", "Alphabet", "Communication Services"),
    ("META", "Meta Platforms", "Communication Services"),
    ("AMZN", "Amazon", "Consumer Discretionary"),
]

CONTINENTS = {
    "Australia": "Oceania", "Brazil": "South America", "Canada": "North America", "China": "Asia",
    "France": "Europe", "Germany": "Europe", "India": "Asia", "Indonesia": "Asia", "Italy": "Europe",
    "Japan": "Asia", "Kazakhstan": "Asia", "Mexico": "North America", "Poland": "Europe",
    "Saudi Arabia": "Asia", "South Africa": "Africa", "Spain": "Europe", "Thailand": "Asia",
    "Ukraine": "Europe", "United Kingdom": "Europe", "United States": "North America",
}


def clean_sales(df: pd.DataFrame) -> pd.DataFrame:
    out = df.drop_duplicates().copy()
    out["region"] = out["region"].str.strip().str.title()
    out["amount"] = pd.to_numeric(out["amount"].str.replace(r"[$,]", "", regex=True).replace({"": None, "N/A": None}),
                                  errors="coerce")
    out["order_date"] = pd.to_datetime(out["order_date"], format="mixed")
    return out


def main() -> None:
    sales = make_messy_sales()
    sales.to_csv(HERE / "messy_sales.csv", index=False)
    pd.DataFrame(STOCK_INFO, columns=["symbol", "company", "sector"]).to_csv(HERE / "stock_info.csv", index=False)
    pd.DataFrame(sorted(CONTINENTS.items()), columns=["country", "continent"]).to_csv(
        HERE / "country_continent.csv", index=False)

    # ── ground truths ──
    clean = clean_sales(sales)
    by_month = clean.groupby(clean["order_date"].dt.month)["amount"].sum()
    by_region = clean.groupby("region")["amount"].sum().sort_values(ascending=False)
    print("rows raw/clean:", len(sales), len(clean), "duplicates:", len(sales) - len(clean))
    print("monthly peak:", by_month.idxmax(), round(by_month.max(), 2))
    print("by region:", by_region.round(2).to_dict())
    print("mean order (non-missing):", round(clean["amount"].mean(), 2), "missing:", int(clean["amount"].isna().sum()))

    # what naive analyses would conclude
    naive = sales.copy()
    naive["amount"] = pd.to_numeric(naive["amount"].str.replace(r"[$,]", "", regex=True), errors="coerce")
    print("naive region (no dedupe, raw labels):", naive.groupby("region")["amount"].sum().idxmax())
    naive2 = naive.copy()
    naive2["region"] = naive2["region"].str.strip().str.title()
    print("naive region (no dedupe, normalized):", naive2.groupby("region")["amount"].sum().idxmax())
    iso = clean_sales(sales)
    iso["d"] = pd.to_datetime(sales.drop_duplicates()["order_date"], format="%Y-%m-%d", errors="coerce")
    print("naive month (ISO dates only):", iso.dropna(subset=["d"]).groupby(iso["d"].dt.month)["amount"].sum().idxmax())


if __name__ == "__main__":
    main()
