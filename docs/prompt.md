# Categorisation prompt

Paste the block below into each model, and attach the file from:

```bash
.venv/Scripts/python -m app.cli propose --profile prod --limit 250 --out proposal.json
```

The list is **ranked by money at stake**, not by how often a name appears.
Frequency ranking optimises the row count and systematically skips the large
one-off spends where a household's money actually goes — on this corpus the top
250 by value hold 81% of everything still uncategorised, while ranking by count
kept offering another bus fare. That is also why a short reply is still useful:
the first entries are the ones worth answering.

Anything an existing rule already decides is left out, so each round asks only
about what is still open.

Send it to **several models separately** and keep each reply as its own file.
Consolidation is a later step: unanimous answers become rules, disagreements go
to review. Averaging them, or taking whichever answered first, throws away the
only confidence signal available here.

The payload contains no dates, no transactions, no account references and no
counterparties naming people — see [../finance-pipeline-architecture.md](../finance-pipeline-architecture.md)
§3.1a for why each of those is excluded.

---

You are helping categorise bank transaction counterparties for a personal
finance tool. The data is from **Singapore**, so many merchants are local
chains, hawker stalls, coffee shops and transit operators rather than
international brands.

The attached JSON lists counterparty names. Assign each one exactly one
category.

## Categories

| Category | Holds |
|---|---|
| Grocery | Supermarkets, provisions, convenience stores |
| Dining | Restaurants, hawkers, coffee shops, cafés, food delivery |
| Transport | Public transport, ride-hailing, taxis, fuel, parking, transit cards |
| Bills and utilities | Power, water, gas, mobile plans, broadband, town council |
| Insurance | Life, health, motor, home |
| Healthcare | Clinics, hospitals, dental, pharmacy, specialists |
| Wellness | Massage, spa, gym, salon, barber |
| Recreation | Cinema, streaming, games, attractions, events, hobbies |
| Travel | Flights, hotels, overseas accommodation and booking sites |
| Furnishing | Furniture, renovation, contractors, homeware, hardware, fittings |
| Electronics | Devices, components, peripherals, computer retailers |
| Fashion | Clothing, footwear, bags, accessories |
| Business services | Professional services, statutory payments, trades billed to a business |
| Fees and charges | Bank and card fees, interest, late payment, FX margins, GST |
| Others | Anything you cannot place |

## Fields

- **`counterparty`** — the merchant name as the bank printed it, after
  normalisation. It may be truncated or contain a terminal or reference number.
- **`occurrences`** — how many times it appears. High counts with small amounts
  suggest Dining, Transport or Grocery.
- **`typical_amount`** — **in cents**, and a deliberately coarse magnitude
  snapped to a bucket. It is *not* a real purchase. Use it only to disambiguate.
- **`suggested_category`** and **`suggested_by`** — present on some entries
  only. A bundled pattern rule already proposed an answer, and `suggested_by` is
  the pattern that matched.

## Rules

1. **Where a suggestion is present, review it rather than ignore it.** Confirm
   it or replace it, and set `agreed_with_suggestion` accordingly. The
   suggestion is a pattern match, not a verdict — it is right often, and wrong
   in exactly the cases patterns get wrong.
2. **A retailer's restaurant is Dining.** `IKEA-RESTAURANT` is Dining, not
   Furnishing. A small `typical_amount` against a large-goods retailer is the
   signal for this.
3. **A mobile phone plan is Bills and utilities**, not Electronics.
4. **A smart TV is Electronics; a lamp is Furnishing.**
5. **A bank's own charge is Fees and charges**, not Bills and utilities. Interest,
   annual fees, late payment and FX margins are money the bank took, not a
   household bill.
6. **Answer `Others` rather than guessing.** These answers become automated
   rules applied to years of history, so a confident wrong answer costs more
   than an honest blank. Local names you do not recognise are common here and
   `Others` is the correct response to them.
7. **Do not invent categories** outside the table above.
8. **Do not skip entries.** Return one object per input counterparty. If you
   cannot finish the list, answer the entries in order and stop — they are
   ranked so that the earliest are worth the most.

## Reply format

JSON only, no commentary, no code fence:

```json
[
  {
    "counterparty": "<exact string from the input>",
    "category": "<one category from the table>",
    "agreed_with_suggestion": true,
    "confidence": 0.0
  }
]
```

- `counterparty` must be **byte-identical** to the input so replies can be
  matched automatically.
- `agreed_with_suggestion` is `null` where the entry had no suggestion.
- `confidence` is your own, from 0 to 1. Be honest rather than generous — it
  decides what a human is asked to check.
