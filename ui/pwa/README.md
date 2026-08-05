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

- **No growth chart.** `/growth` returns `501`, because net worth over time must
  come from statement balances and summing transactions would make a transfer
  between your own accounts look like growth. The overview says so rather than
  drawing a plausible line, and there is no arrow or percentage yet — the
  calm-or-concern signal in §5.1(D) waits on a real number.
- **Decisions are not applied.** The review screen records them; writing them
  through the ledger is still a CLI pass.
- **No authentication.** There is none in the API yet. Do not expose either
  beyond a trusted network.
- **Not yet a PWA.** No manifest or service worker, so no home-screen install
  and no offline read.
- **No `/tv` route.** The 10-foot layout in §5 is not built.

## What has been verified

`npm install` and `npm run build` both pass, and the endpoints this client calls
were exercised against a running API against real data — `/health`, `/summary`
with a date range, and `/review`.

Not verified: the rendered screens. Nothing here has been opened in a browser,
so layout, responsiveness and the interaction on the review table are unproven.
