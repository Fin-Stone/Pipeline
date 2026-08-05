/**
 * The consolidated view, architecture §5.1(A) and (B).
 *
 * One screen across every account, filterable by institution, category and
 * date range, with the per-month/week/day averages computed over **the same
 * range as the totals** — the server guarantees that, and this screen shows the
 * range so the two halves can never appear to describe different periods.
 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert, AlertTitle, Box, Card, CardContent, Chip, CircularProgress, Divider,
  FormControl, Grid, InputLabel, LinearProgress, MenuItem, Select, Stack,
  TextField, Typography,
} from "@mui/material";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import UndoIcon from "@mui/icons-material/Undo";
import { Button, IconButton, List, ListItem, ListItemText, Tooltip } from "@mui/material";
import { Account, ApiError, Category, Filters, Summary, Trend as TrendData, Txn, api } from "./api";
import Trend from "./Trend";
import { magnitude, money, monthsAgo, today } from "./money";

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card variant="outlined" sx={{ height: "100%" }}>
      <CardContent>
        <Typography variant="overline" color="text.secondary">{label}</Typography>
        <Typography variant="h5" sx={{ fontVariantNumeric: "tabular-nums", mt: 0.5 }}>
          {value}
        </Typography>
        {hint && <Typography variant="caption" color="text.secondary">{hint}</Typography>}
      </CardContent>
    </Card>
  );
}

export default function Dashboard() {
  const [months, setMonths] = useState(6);
  const [since, setSince] = useState(monthsAgo(6));
  const [until, setUntil] = useState(today());
  const [accountIds, setAccountIds] = useState<number[]>([]);
  const [categoryNames, setCategoryNames] = useState<string[]>([]);

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [trend, setTrend] = useState<TrendData | null>(null);
  const [txns, setTxns] = useState<Txn[]>([]);
  // Session hiding: forgotten on refresh, by design. It is a way to ask "what
  // would this look like without that", not a decision about the ledger.
  const [muted, setMuted] = useState<number[]>([]);
  const [growthUnavailable, setGrowthUnavailable] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // The taxonomy is tenant data. It is fetched, never hardcoded, because a
    // household may rename or add to it at any time.
    Promise.all([api.accounts(), api.categories()])
      .then(([a, c]) => { setAccounts(a.accounts); setCategories(c.categories); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));
  }, []);

  useEffect(() => {
    setLoading(true);
    // The muted ids go to the server with everything else, so the total, the
    // averages, the bars and the list all describe the same set of rows.
    const filters: Filters = {
      since, until, account_id: accountIds, category: categoryNames,
      exclude_txn_id: muted,
    };
    Promise.all([api.summary(filters), api.trend(filters), api.transactions(filters, 60)])
      .then(([s, t, list]) => { setSummary(s); setTrend(t); setTxns(list.transactions); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)))
      .finally(() => setLoading(false));
  }, [since, until, accountIds, categoryNames, muted]);

  async function hideForGood(id: number) {
    await api.hide(id);
    // Dropped from the session list too, or it would be excluded twice and
    // reappear the moment the persistent hide were undone.
    setMuted((m) => m.filter((x) => x !== id));
    setTxns((t) => t.filter((x) => x.id !== id));
  }

  useEffect(() => {
    // Growth is not implemented server-side, and the server says so rather than
    // returning a series computed the wrong way. Surfaced as an honest gap: a
    // fabricated trend is the one thing worse than a missing one here, because
    // §5.1(D) attaches an emotional signal to it.
    api.growth(months)
      .then(() => setGrowthUnavailable(null))
      .catch((e) => setGrowthUnavailable(
        e instanceof ApiError && typeof e.detail === "object" && e.detail !== null
          ? String((e.detail as Record<string, unknown>).reason ?? "unavailable")
          : "unavailable",
      ));
  }, [months]);

  const spend = summary?.total_minor ?? 0;
  const biggest = useMemo(
    () => (summary?.by_category ?? []).filter((r) => r.total_minor < 0),
    [summary],
  );
  const largest = biggest.length ? Math.abs(biggest[0].total_minor) : 1;

  if (error) return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;

  return (
    <Stack spacing={2}>
      <Alert severity="info" icon={<TrendingDownIcon />}>
        <AlertTitle>Net worth over time is not available yet</AlertTitle>
        {growthUnavailable ?? "Checking…"} — so this screen shows spending only,
        and no growth arrow or percentage is displayed rather than one derived
        the wrong way.
      </Alert>

      <Card variant="outlined">
        <CardContent>
          <Stack direction={{ xs: "column", md: "row" }} spacing={2} flexWrap="wrap">
            <FormControl size="small" sx={{ minWidth: 130 }}>
              <InputLabel>Window</InputLabel>
              <Select
                label="Window" value={months}
                onChange={(e) => {
                  const m = Number(e.target.value);
                  setMonths(m); setSince(monthsAgo(m)); setUntil(today());
                }}
              >
                {[1, 3, 6, 12, 24].map((m) => (
                  <MenuItem key={m} value={m}>{m} month{m > 1 ? "s" : ""}</MenuItem>
                ))}
              </Select>
            </FormControl>
            <TextField size="small" type="date" label="From" InputLabelProps={{ shrink: true }}
              value={since} onChange={(e) => setSince(e.target.value)} />
            <TextField size="small" type="date" label="To" InputLabelProps={{ shrink: true }}
              value={until} onChange={(e) => setUntil(e.target.value)} />
            <FormControl size="small" sx={{ minWidth: 180 }}>
              <InputLabel>Accounts</InputLabel>
              <Select
                multiple label="Accounts" value={accountIds}
                onChange={(e) => setAccountIds(e.target.value as number[])}
                renderValue={(ids) => `${ids.length || "All"} selected`}
              >
                {accounts.map((a) => (
                  <MenuItem key={a.id} value={a.id}>
                    {a.institution} — {a.account_ref_masked}
                    {a.sub_account_label ? ` / ${a.sub_account_label}` : ""}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 180 }}>
              <InputLabel>Categories</InputLabel>
              <Select
                multiple label="Categories" value={categoryNames}
                onChange={(e) => setCategoryNames(e.target.value as string[])}
                renderValue={(names) => `${names.length || "All"} selected`}
              >
                {categories.map((c) => (
                  <MenuItem key={c.id} value={c.name}>{c.name}</MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>
        </CardContent>
      </Card>

      {loading && <LinearProgress />}

      <Grid container spacing={2}>
        <Grid item xs={12} sm={6} md={3}>
          <Tile label="Spent" value={magnitude(spend)}
            hint={summary?.range.days ? `over ${summary.range.days} days` : "open range"} />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Tile label="Per month" value={magnitude(summary?.average_minor.per_month)}
            hint="same range as above" />
        </Grid>
        <Grid item xs={6} md={3}>
          <Tile label="Per week" value={magnitude(summary?.average_minor.per_week)} />
        </Grid>
        <Grid item xs={6} md={3}>
          <Tile label="Per day" value={magnitude(summary?.average_minor.per_day)} />
        </Grid>
      </Grid>

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="h6">Spending over time</Typography>
            {muted.length > 0 && (
              <Button size="small" startIcon={<UndoIcon />} onClick={() => setMuted([])}>
                Show {muted.length} hidden again
              </Button>
            )}
          </Stack>
          <Divider sx={{ my: 1.5 }} />
          {trend ? <Trend data={trend} /> : <CircularProgress size={20} />}
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6">Transactions</Typography>
          <Typography variant="caption" color="text.secondary">
            Hiding removes a row from the totals and the bars above, not just
            from this list. The eye icon lasts until you refresh; “forever”
            keeps it hidden and is undone on the Hidden page.
          </Typography>
          <Divider sx={{ my: 1 }} />
          <List dense sx={{ maxHeight: 340, overflowY: "auto" }}>
            {txns.map((t) => (
              <ListItem
                key={t.id} divider disableGutters
                secondaryAction={
                  <Stack direction="row" spacing={0.5} alignItems="center">
                    <Typography sx={{ fontVariantNumeric: "tabular-nums", mr: 1 }}>
                      {money(t.amount_minor)}
                    </Typography>
                    <Tooltip title="Hide for this session">
                      <IconButton size="small"
                        onClick={() => setMuted((m) => [...m, t.id])}>
                        <VisibilityOffIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Button size="small" onClick={() => hideForGood(t.id)}>forever</Button>
                  </Stack>
                }
              >
                <ListItemText
                  primary={t.counterparty_norm || "(no counterparty)"}
                  secondary={`${t.posted_date} · ${t.institution} · ${t.category ?? "uncategorised"}`}
                />
              </ListItem>
            ))}
            {txns.length === 0 && (
              <Typography color="text.secondary">Nothing in this range.</Typography>
            )}
          </List>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" gutterBottom>Where it went</Typography>
          <Typography variant="caption" color="text.secondary">
            Transfers between your own accounts are already excluded.
          </Typography>
          <Divider sx={{ my: 1.5 }} />
          {!summary && <CircularProgress size={20} />}
          <Stack spacing={1.5}>
            {biggest.map((row) => (
              <Box key={row.category ?? "uncategorised"}>
                <Stack direction="row" justifyContent="space-between" alignItems="baseline">
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography>{row.category ?? "Uncategorised"}</Typography>
                    {row.category === null && (
                      <Chip size="small" color="warning" variant="outlined"
                        label="needs rules" />
                    )}
                  </Stack>
                  <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
                    {money(row.total_minor)}
                  </Typography>
                </Stack>
                <LinearProgress
                  variant="determinate"
                  value={Math.min(100, (Math.abs(row.total_minor) / largest) * 100)}
                  sx={{ height: 6, borderRadius: 3, mt: 0.5 }}
                />
                <Typography variant="caption" color="text.secondary">
                  {row.rows} transaction{row.rows === 1 ? "" : "s"}
                </Typography>
              </Box>
            ))}
            {summary && biggest.length === 0 && (
              <Typography color="text.secondary">Nothing in this range.</Typography>
            )}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
