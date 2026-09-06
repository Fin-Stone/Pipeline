/**
 * The consolidated view, architecture §5.1(A) and (B).
 *
 * One screen across every account, filterable by institution, category and
 * date range, with the per-month/week/day averages computed over **the same
 * range as the totals** — the server guarantees that, and this screen shows the
 * range so the two halves can never appear to describe different periods.
 *
 * Money out and money in are the same screen twice, so this is one component
 * with a `mode`. They differ in wording and in which side of zero they read,
 * not in structure, and a second copy would only drift from this one.
 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert, Box, Card, CardContent, Chip, CircularProgress, Divider,
  FormControl, Grid, InputLabel, LinearProgress, MenuItem, Select, Snackbar,
  Stack, TextField, ToggleButton, ToggleButtonGroup, Typography,
} from "@mui/material";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import UndoIcon from "@mui/icons-material/Undo";
import SearchIcon from "@mui/icons-material/Search";
import ClearIcon from "@mui/icons-material/Clear";
import {
  Button, IconButton, InputAdornment, List, ListItem, ListItemText, Tooltip,
} from "@mui/material";
import {
  Account, ApiError, Category, Direction, Filters, Summary, Trend as TrendData, Txn, api,
} from "./api";
import PaybackDialog from "./PaybackDialog";
import RecurringDialog from "./RecurringDialog";
import Trend from "./Trend";
import { magnitude, money, monthsAgo, today } from "./money";

export type Mode = "spending" | "income";

const WORDING = {
  spending: {
    total: "Spent",
    overTime: "Spending over time",
    breakdown: "Where it went",
    empty: "Nothing spent in this range.",
    measure: "out_minor" as const,
    direction: "out" as Direction,
  },
  income: {
    total: "Earned",
    overTime: "Income over time",
    breakdown: "Where it came from",
    empty: "Nothing received in this range.",
    measure: "in_minor" as const,
    direction: "in" as Direction,
  },
};

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

export default function Dashboard({ mode }: { mode: Mode }) {
  const words = WORDING[mode];

  const [months, setMonths] = useState(6);
  const [since, setSince] = useState(monthsAgo(6));
  const [until, setUntil] = useState(today());
  const [accountIds, setAccountIds] = useState<number[]>([]);
  const [categoryNames, setCategoryNames] = useState<string[]>([]);
  // Two pieces of state for one box: what has been typed, and what has been
  // asked for. Firing a request per keystroke would put five half-typed
  // searches in flight and let the slowest one win.
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [linking, setLinking] = useState<Txn | null>(null);
  const [marking, setMarking] = useState<Txn | null>(null);
  // Income only: the bars can show what came in, or what was left after it all
  // went out again. The second is the number that answers "are we ahead".
  const [measure, setMeasure] = useState<"in_minor" | "net_minor">("in_minor");

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [net, setNet] = useState<Summary | null>(null);
  const [trend, setTrend] = useState<TrendData | null>(null);
  const [txns, setTxns] = useState<Txn[]>([]);
  // Session hiding: forgotten on refresh, by design. It is a way to ask "what
  // would this look like without that", not a decision about the ledger.
  const [muted, setMuted] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloads, setReloads] = useState(0);
  const [undoable, setUndoable] = useState<
    { message: string; undo: () => Promise<unknown> } | null
  >(null);

  useEffect(() => {
    // The taxonomy is tenant data. It is fetched, never hardcoded, because a
    // household may rename or add to it at any time.
    Promise.all([api.accounts(), api.categories()])
      .then(([a, c]) => { setAccounts(a.accounts); setCategories(c.categories); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(typed.trim()), 300);
    return () => clearTimeout(timer);
  }, [typed]);

  useEffect(() => {
    setLoading(true);
    // The muted ids go to the server with everything else, so the total, the
    // averages, the bars and the list all describe the same set of rows. `q`
    // goes with them for the same reason — the server drops the date range when
    // it is set, and it has to do that for every figure at once or the screen
    // starts contradicting itself.
    const filters: Filters = {
      since, until, account_id: accountIds, category: categoryNames,
      exclude_txn_id: muted, q: query || undefined,
    };
    Promise.all([
      api.summary(filters, words.direction),
      api.trend(filters),
      api.transactions(filters, 60, words.direction),
      // What is left over, on the same range and the same exclusions. Fetched
      // rather than subtracted here: this client never does arithmetic on money.
      mode === "income" ? api.summary(filters, "net") : Promise.resolve(null),
    ])
      .then(([s, t, list, n]) => {
        setSummary(s); setTrend(t); setTxns(list.transactions); setNet(n);
      })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)))
      .finally(() => setLoading(false));
  }, [since, until, accountIds, categoryNames, muted, mode, words.direction, query, reloads]);

  /** Bump to re-run the effect above after a write the server owns. */
  function reload() {
    setReloads((n) => n + 1);
  }

  async function recategorise(id: number, name: string) {
    const row = txns.find((t) => t.id === id);
    const was = row?.category ?? null;
    const wasHuman = row?.source === "human";

    // A named, uncategorised row is not a one-off correction: it is the same
    // merchant decision offered on Recurring and Review. Store it as a rule so
    // every matching row on this screen, the rest of the ledger, and future
    // imports agree. Keep existing human corrections individual: they are the
    // explicit exception to a merchant rule, and the rule pass must not erase
    // that distinction. Rows with no payee also have nothing a rule can match.
    if (name !== "" && row?.counterparty_norm && !wasHuman) {
      const counterparty = row.counterparty_norm;
      const decided = await api.decide(counterparty, name);
      const wanted = counterparty.toUpperCase();
      setTxns((t) => t.map((x) => (
        x.counterparty_norm.toUpperCase() === wanted && x.source !== "human"
          ? { ...x, category: decided.category, source: "rule" }
          : x
      )));
      reload();

      offerUndo(
        `Filed every ${counterparty} transaction under ${decided.category}`,
        async () => {
          await api.undecide(counterparty);
          // `decide` replaces an earlier operator decision. Put that decision
          // back on undo; imported rules need no help because undeciding lets
          // the rule pass expose them again automatically.
          const previous = decided.replaced.at(0);
          if (previous) await api.decide(counterparty, previous.category);
          reload();
        },
      );
      return;
    }

    if (name === "") {
      // Back to uncategorised, which the automatic pass may then speak about
      // again. Without this, a correction could be changed but never taken
      // back, and "I should not have touched that one" had no answer.
      await api.clearCategory(id);
      setTxns((t) => t.map((x) => (x.id === id ? { ...x, category: null, source: null } : x)));
    } else {
      // Marked source 'human' server-side, which the rule pass will not overwrite.
      await api.setCategory(id, name);
      setTxns((t) => t.map((x) => (x.id === id ? { ...x, category: name, source: "human" } : x)));
    }
    reload();

    offerUndo(
      name === "" ? "Category cleared" : `Filed under ${name}`,
      async () => {
        // Restores what was there, including the fact that nothing was.
        if (was === null || !wasHuman) await api.clearCategory(id);
        else await api.setCategory(id, was);
        setTxns((t) => t.map((x) => (
          x.id === id ? { ...x, category: was, source: wasHuman ? "human" : null } : x
        )));
        reload();
      },
    );
  }

  async function addCategory() {
    const name = window.prompt("New category name");
    if (!name?.trim()) return;
    const cleaned = name.trim();
    try {
      await api.addCategory(cleaned);
      setCategories((await api.categories()).categories);
      // Removable while nothing uses it, which is what makes adding one safe
      // to try. The server refuses once anything references it.
      offerUndo(`Added ${cleaned}`, async () => {
        await api.deleteCategory(cleaned);
        setCategories((await api.categories()).categories);
      });
    } catch (e) {
      setError(String(e instanceof ApiError ? e.detail : e));
    }
  }

  /** Every figure-changing action offers the way back immediately.
   *
   *  These buttons sit inches apart on a dense list, so the misclick is the
   *  normal case rather than the exotic one. The Excluded tab is the durable
   *  route back; this is the one for the half-second after, when the row has
   *  just vanished and the person still remembers what they meant to press. */
  function offerUndo(message: string, undo: () => Promise<unknown>) {
    setUndoable({ message, undo });
  }

  async function markTransfer(id: number) {
    // Not spending, and not income either: a movement between the operator's
    // own accounts that the matcher could not prove. Excluded from every
    // figure, and it survives a re-run of the matcher because a person decided.
    await api.markTransfer(id);
    setTxns((t) => t.filter((x) => x.id !== id));
    reload();
    offerUndo("Marked as a transfer", async () => {
      await api.unmarkTransfer(id);
      reload();
    });
  }

  async function hideForGood(id: number) {
    await api.hide(id);
    // Dropped from the session list too, or it would be excluded twice and
    // reappear the moment the persistent hide were undone.
    setMuted((m) => m.filter((x) => x !== id));
    setTxns((t) => t.filter((x) => x.id !== id));
    reload();
    offerUndo("Hidden from every figure", async () => {
      await api.unhide(id);
      reload();
    });
  }

  const breakdown = useMemo(
    () => (summary?.by_category ?? []).filter(
      (r) => (mode === "income" ? r.total_minor > 0 : r.total_minor < 0),
    ),
    [summary, mode],
  );
  const largest = breakdown.length ? Math.abs(breakdown[0].total_minor) : 1;
  const kept = net?.total_minor ?? null;

  if (error) return <Alert severity="error" sx={{ m: 2 }}>{error}</Alert>;

  return (
    <Stack spacing={2}>
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
            <TextField
              size="small" label="Search" placeholder="merchant, or text off the statement"
              value={typed} onChange={(e) => setTyped(e.target.value)}
              sx={{ minWidth: 240, flexGrow: 1 }}
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
                ),
                endAdornment: typed ? (
                  <InputAdornment position="end">
                    <IconButton size="small" onClick={() => setTyped("")}>
                      <ClearIcon fontSize="small" />
                    </IconButton>
                  </InputAdornment>
                ) : null,
              }}
            />
          </Stack>
        </CardContent>
      </Card>

      {query && (
        // Said plainly, because the alternative is a search that silently
        // returns nothing for a charge two years old and looks like an answer.
        <Alert severity="info" icon={<SearchIcon />}>
          Searching the whole ledger for “{query}” — the date range above is
          ignored. Everything on this page describes the matches.
        </Alert>
      )}

      {loading && <LinearProgress />}

      <Grid container spacing={2}>
        <Grid item xs={12} sm={6} md={3}>
          <Tile label={words.total} value={magnitude(summary?.total_minor)}
            hint={summary?.range.days ? `over ${summary.range.days} days` : "open range"} />
        </Grid>
        {mode === "income" && (
          <Grid item xs={12} sm={6} md={3}>
            {/* Signed, unlike every other tile: a household in deficit needs to
                see the minus, not an unmarked magnitude. */}
            <Tile label="Kept" value={money(kept)}
              hint={kept === null ? undefined : kept < 0 ? "spent more than earned" : "after spending"} />
          </Grid>
        )}
        <Grid item xs={12} sm={6} md={3}>
          <Tile label="Per month" value={magnitude(summary?.average_minor.per_month)}
            hint="same range as above" />
        </Grid>
        <Grid item xs={6} md={3}>
          <Tile label="Per week" value={magnitude(summary?.average_minor.per_week)} />
        </Grid>
        {mode === "spending" && (
          <Grid item xs={6} md={3}>
            <Tile label="Per day" value={magnitude(summary?.average_minor.per_day)} />
          </Grid>
        )}
      </Grid>

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="center"
            flexWrap="wrap" gap={1}>
            <Typography variant="h6">{words.overTime}</Typography>
            <Stack direction="row" spacing={1} alignItems="center">
              {mode === "income" && (
                <ToggleButtonGroup
                  size="small" exclusive value={measure}
                  onChange={(_, v) => v && setMeasure(v)}
                >
                  <ToggleButton value="in_minor">Income</ToggleButton>
                  <ToggleButton value="net_minor">Net</ToggleButton>
                </ToggleButtonGroup>
              )}
              {muted.length > 0 && (
                <Button size="small" startIcon={<UndoIcon />} onClick={() => setMuted([])}>
                  Show {muted.length} hidden again
                </Button>
              )}
            </Stack>
          </Stack>
          <Divider sx={{ my: 1.5 }} />
          {trend
            ? <Trend data={trend} measure={mode === "income" ? measure : words.measure} />
            : <CircularProgress size={20} />}
          {mode === "income" && measure === "net_minor" && (
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
              Green is a surplus, blue a deficit. The line is the trailing
              average, so it answers whether the household is trending up or
              down rather than how one period happened to land.
            </Typography>
          )}
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="h6">Transactions</Typography>
            <Button size="small" onClick={addCategory}>+ category</Button>
          </Stack>
          <Typography variant="caption" color="text.secondary">
            Filing an uncategorised merchant updates every matching transaction
            and future imports, just like Recurring and Review. Existing one-off
            corrections and rows with no payee stay individual. Hiding removes a
            row from the totals and bars above, not just from this list — the eye
            icon lasts until refresh, “forever” is undone on the Hidden page.
          </Typography>
          <Divider sx={{ my: 1 }} />
          <List dense sx={{ maxHeight: 340, overflowY: "auto" }}>
            {txns.map((t) => (
              <ListItem
                key={t.id} divider disableGutters
                secondaryAction={
                  <Stack direction="row" spacing={0.5} alignItems="center">
                    {/* Both figures when part of it came back. The original is
                        struck through rather than replaced: a number that
                        silently disagrees with the statement is unauditable. */}
                    {t.paid_back_minor !== 0 && (
                      <Typography
                        sx={{
                          fontVariantNumeric: "tabular-nums",
                          textDecoration: "line-through",
                          color: "text.disabled", fontSize: 13,
                        }}
                      >
                        {money(t.amount_minor)}
                      </Typography>
                    )}
                    <Typography sx={{ fontVariantNumeric: "tabular-nums", mr: 1 }}>
                      {money(t.effective_amount_minor)}
                    </Typography>
                    <Tooltip title="Hide for this session">
                      <IconButton size="small"
                        onClick={() => setMuted((m) => [...m, t.id])}>
                        <VisibilityOffIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    {t.amount_minor < 0 && (
                      <Tooltip title="Someone paid you back for part of this">
                        <Button size="small" onClick={() => setLinking(t)}>
                          {t.paid_back_minor !== 0 ? "paybacks ✓" : "paybacks"}
                        </Button>
                      </Tooltip>
                    )}
                    {t.amount_minor < 0 && (
                      <Tooltip title="This repeats — a premium or a plan the detector cannot see yet">
                        <Button size="small" onClick={() => setMarking(t)}>
                          recurring
                        </Button>
                      </Tooltip>
                    )}
                    <Tooltip title="Not real money in or out — a move between your own accounts">
                      <Button size="small" onClick={() => markTransfer(t.id)}>
                        transfer
                      </Button>
                    </Tooltip>
                    <Button size="small" onClick={() => hideForGood(t.id)}>hide</Button>
                  </Stack>
                }
              >
                <ListItemText
                  primary={t.counterparty_norm || "(no counterparty)"}
                  secondary={
                    <Stack direction="row" spacing={1} alignItems="center"
                      sx={{ mt: 0.5 }} flexWrap="wrap">
                      <Typography variant="caption" color="text.secondary">
                        {t.posted_date} · {t.institution}
                      </Typography>
                      {/* What the statement actually printed, when it differs.
                          Normalisation strips references and mechanism words,
                          so this is often the text the operator remembers. */}
                      {t.description_raw && t.description_raw !== t.counterparty_norm && (
                        <Typography variant="caption" color="text.disabled" noWrap
                          sx={{ maxWidth: 260 }}>
                          {t.description_raw}
                        </Typography>
                      )}
                      <Select
                        size="small" variant="standard" displayEmpty
                        value={t.category ?? ""}
                        onChange={(e) => recategorise(t.id, e.target.value)}
                        sx={{ fontSize: 12, minWidth: 130 }}
                      >
                        {/* Selectable, not just a placeholder: a correction you
                            can make but never take back is not a correction. */}
                        <MenuItem value="">
                          <em>uncategorised</em>
                        </MenuItem>
                        {categories.map((c) => (
                          <MenuItem key={c.id} value={c.name}>{c.name}</MenuItem>
                        ))}
                      </Select>
                      {t.source === "human" && (
                        <Chip size="small" variant="outlined" color="success" label="yours" />
                      )}
                    </Stack>
                  }
                />
              </ListItem>
            ))}
            {txns.length === 0 && (
              <Typography color="text.secondary">
                {query ? `Nothing matches “${query}”.` : words.empty}
              </Typography>
            )}
          </List>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" gutterBottom>{words.breakdown}</Typography>
          <Typography variant="caption" color="text.secondary">
            Transfers between your own accounts are already excluded.
          </Typography>
          <Divider sx={{ my: 1.5 }} />
          {!summary && <CircularProgress size={20} />}
          <Stack spacing={1.5}>
            {breakdown.map((row) => (
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
            {summary && breakdown.length === 0 && (
              <Typography color="text.secondary">{words.empty}</Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

      {linking && (
        <PaybackDialog
          charge={linking}
          onClose={() => setLinking(null)}
          onLinked={(message, undo) => { reload(); offerUndo(message, undo); }}
        />
      )}

      {marking && (
        <RecurringDialog
          charge={marking}
          onClose={() => setMarking(null)}
          onMarked={(message, undo) => { reload(); offerUndo(message, undo); }}
        />
      )}

      {/* The way back from the click just made. The Excluded tab is the durable
          route; this is the one for the moment the row disappears and the
          person still remembers what they meant to press. */}
      <Snackbar
        open={undoable !== null}
        autoHideDuration={10000}
        onClose={() => setUndoable(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
        message={undoable?.message}
        action={
          <Button
            size="small" color="secondary"
            onClick={async () => {
              const pending = undoable;
              setUndoable(null);
              if (pending) await pending.undo();
            }}
          >
            Undo
          </Button>
        }
      />
    </Stack>
  );
}
