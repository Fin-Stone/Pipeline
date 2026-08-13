# Finstone PWA

The finance UI. Three screens: overview, recurring payments, review queue.

```bash
cd ui/pwa
npm install
npm run dev          # http://localhost:5173
```

Run the API alongside it:

```bash
pip install -e .[api]
uvicorn app.api.main:app --reload
```

On first load the app asks for a server address and stores it locally. It also
checks `GET /health` and **refuses to connect** if the server speaks a different
API version, rather than attempting and misreporting.

## Rules this client is built under

**Read [../../docs/api-contracts.md](../../docs/api-contracts.md) before
changing anything that talks to the server.** It is the agreement, not a
description of the current implementation.

- **Nothing is imported from `app/`.** This client speaks HTTP and shares
  nothing else, so it can be pointed at any server running the same API
  version — architecture §5.2.
- **No endpoint is hardcoded.** The server is the user's choice: their own
  install or a hosted one, entered at sign-in. A build that knows where its
  server lives cannot be self-hosted.
- **Money is integer minor units** all the way to the formatter in
  `src/money.ts`. Nothing does arithmetic on a converted value — sum in minor
  units and format last, or the screen the user trusts reintroduces exactly the
  rounding error the ledger exists to avoid.
- **The taxonomy is fetched, never hardcoded.** Categories are tenant data and a
  household may rename or add to them.
- **Null is not zero.** An average over an open-ended range has no value, and
  the UI renders `—` rather than `0.00`.

## Known gaps

- **Not yet a PWA.** No manifest or service worker, so no home-screen install
  and no offline read. The name is aspirational until then.
- **No `/tv` route.** The 10-foot layout in §5 is not built, and there is no
  router here at all — the screens are tabs.
- **No authentication.** There is none in the API yet. Do not expose either
  beyond a trusted network.
- **Nothing surfaces reconciliation.** `GET /reconciliation` says whether the
  ledger still agrees with the balances the banks declared, and no screen asks
  it yet.

Two entries that used to be here are done, and are recorded because the shape
of the fix is the useful part:

- The growth chart exists. `/growth` derives net worth from declared statement
  balances rather than by summing transactions — which would read a transfer
  between your own accounts as growth on one side and loss on the other — and
  `NetWorth.tsx` carries the arrow and percentage §5.1(D) asks for, with `—`
  where the window opened at zero or in debt and a percentage would mean
  nothing.
- Decisions reach the ledger. `POST /review/decide` applies the rules before it
  returns and says how many rows moved, so a decision is true on every screen
  the moment it is made.

## What has been verified

`npm run build` passes, which includes `tsc -b`, and the endpoints this client
calls were exercised against a running API on real data.

Not verified: the rendered screens. Nothing here has been opened in a browser,
so layout, responsiveness and the interaction on the review table are unproven.
