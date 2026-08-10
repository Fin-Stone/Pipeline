/**
 * The recurring payments page, architecture §5.1(C).
 *
 * Four purposes, and the layout serves them in order: what the commitment
 * totals, what is coming, what did not arrive, and what a subscription's price
 * has done. Lapsed series are shown apart from overdue ones because a
 * cancelled subscription and a skipped payment want opposite reactions — one
 * should go quiet, the other should be raised.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Card, CardContent, Chip, Divider, FormControl, IconButton,
  LinearProgress, List, ListItem, ListItemText, MenuItem, Select, Stack,
  Tooltip, Typography,
} from "@mui/material";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import CloseIcon from "@mui/icons-material/Close";
import UndoIcon from "@mui/icons-material/Undo";
import {
  ApiError, Category, Dismissed, Marked, Recurring as RecurringData, Series, api,
} from "./api";
import { magnitude } from "./money";

/**
 * One series, with the category it falls under editable in place.
 *
 * A series is a merchant, and a merchant's category is a decision — the same
 * decision the review queue makes. So this calls the same route rather than
 * inventing a second way to set one thing, which is how two places come to
 * disagree. Correcting it here reaches every row of the series and every other
 * transaction from that merchant at once.
 */
function SeriesRow({ s, note, categories, onCategorise, onDismiss }: {
  s: Series;
  note?: string;
  categories: Category[];
  onCategorise: (merchant: string, category: string) => Promise<void>;
  onDismiss: (s: Series) => Promise<void>;
}) {
  const change = s.price_changes.at(-1);
  const cheaper = change ? change.to_minor < change.from_minor : false;
  const [saving, setSaving] = useState(false);

  return (
    <ListItem divider disableGutters
      secondaryAction={
        <Stack direction="row" spacing={1} alignItems="center">
          <Stack alignItems="flex-end">
            <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
              {magnitude(s.amount_centre_minor)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {magnitude(s.monthly_equivalent_minor)}/mo
            </Typography>
          </Stack>
          {/* Dismissing what was detected and un-marking what a person marked
              are different acts on different things, so the button says which
              — offering to "dismiss" somebody's own mark would read as the
              server disagreeing with them. Both are one click, and both are
              listed below where they can be taken back. */}
          <Tooltip title={s.marked_by === "operator"
            ? "You marked this as recurring — take it back"
            : "Not a subscription"}>
            <IconButton size="small" onClick={() => onDismiss(s)}>
              <CloseIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        </Stack>
      }
    >
      <ListItemText
        primary={
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            <Typography component="span">{s.merchant}</Typography>
            <Chip size="small" variant="outlined" label={s.period_label} />
            {/* Said out loud, because the period on one of these is an
                assertion rather than a measurement, and a reader deciding
                whether to trust the monthly total needs to know which. */}
            {s.marked_by === "operator" && (
              <Tooltip title="You said this repeats. The detector has not seen enough payments to tell.">
                <Chip size="small" variant="outlined" color="primary" label="marked by you" />
              </Tooltip>
            )}
            {change && (
              // A price change is a fact about a subscription, not a new one,
              // so it is shown on the series rather than as a second entry.
              // A cut is as worth seeing as a rise: it is the evidence that a
              // plan change actually took effect.
              <Tooltip title={`Changed on ${change.on}`}>
                <Chip size="small" color={cheaper ? "success" : "warning"}
                  icon={cheaper ? <ArrowDownwardIcon /> : <ArrowUpwardIcon />}
                  label={`${magnitude(change.from_minor)} → ${magnitude(change.to_minor)}`} />
              </Tooltip>
            )}
            {s.grouped_by === "amount" ? (
              // No dropdown, because there is nothing to decide *about*: these
              // rows were gathered by amount precisely because the bank printed
              // no payee, and the name shown is this server's label. A rule
              // written against it would match no transaction while the chip
              // read back as settled — which is worse than saying so.
              <Tooltip title="The bank printed no payee on these rows, so there is no merchant to file. Categorise the transactions themselves on the spending page.">
                <Chip size="small" variant="outlined" color="default"
                  label="no payee to file" />
              </Tooltip>
            ) : (
            <FormControl size="small" variant="standard" sx={{ minWidth: 130 }}>
              <Select
                value={s.category ?? ""}
                displayEmpty
                disabled={saving}
                onChange={async (e) => {
                  setSaving(true);
                  try {
                    await onCategorise(s.merchant, e.target.value as string);
                  } finally {
                    setSaving(false);
                  }
                }}
                renderValue={(v) =>
                  v ? String(v) : <em style={{ opacity: 0.6 }}>Uncategorised</em>
                }
              >
                {categories.map((c) => (
                  <MenuItem key={c.name} value={c.name}>{c.name}</MenuItem>
                ))}
              </Select>
            </FormControl>
            )}
          </Stack>
        }
        secondary={
          note ??
          `${s.occurrences} payments · ${magnitude(s.total_paid_minor)} to date · next ${s.expected_next}`
        }
      />
    </ListItem>
  );
}

export default function Recurring() {
  const [data, setData] = useState<RecurringData | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [dismissed, setDismissed] = useState<Dismissed[]>([]);
  const [marked, setMarked] = useState<Marked[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([
      api.recurring(), api.categories(), api.dismissedRecurring(), api.markedRecurring(),
    ])
      .then(([r, c, d, m]) => {
        setData(r); setCategories(c.categories);
        setDismissed(d.dismissed); setMarked(m.marked);
      })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));
  }, []);

  useEffect(load, [load]);

  // Reloaded rather than patched in place: a decision changes what every
  // figure on this page says, and a row that updated while the monthly
  // commitment above it did not would be a page disagreeing with itself.
  const categorise = useCallback(async (merchant: string, category: string) => {
    await api.decide(merchant, category);
    load();
  }, [load]);

  // One button, two inverses. Dismissing a series a person marked would leave
  // the mark in place and hide its own result — the page would go on believing
  // in a subscription nobody could see, and the monthly commitment would still
  // count it. So the act taken back is the act that was made.
  const dismiss = useCallback(async (s: Series) => {
    if (s.marked_by === "operator") {
      await api.unmarkRecurring(s.merchant, s.amount_centre_minor);
    } else {
      await api.dismissRecurring(s.merchant, s.amount_centre_minor);
    }
    load();
  }, [load]);

  const restore = useCallback(async (d: Dismissed) => {
    await api.restoreRecurring(d.merchant_norm, d.amount_centre_minor);
    load();
  }, [load]);

  const unmark = useCallback(async (m: Marked) => {
    await api.unmarkRecurring(m.merchant_norm, m.amount_centre_minor);
    load();
  }, [load]);

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!data) return <LinearProgress />;

  return (
    <Stack spacing={2}>
      <Card variant="outlined">
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            Committed every month
          </Typography>
          <Typography variant="h4" sx={{ fontVariantNumeric: "tabular-nums" }}>
            {magnitude(data.monthly_commitment_minor)}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            Yearly and quarterly commitments are scaled to a month so they compare.
          </Typography>
        </CardContent>
      </Card>

      {data.overdue.length > 0 && (
        <Alert severity="warning">
          <strong>{data.overdue.length} expected and not seen.</strong> Either a
          payment was missed, or it is late.
        </Alert>
      )}

      {data.due_soon.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6">Due soon</Typography>
            <List dense>
              {data.due_soon.map((s) => (
                <SeriesRow key={s.merchant} s={s}
                  categories={categories} onCategorise={categorise} onDismiss={dismiss} />
              ))}
            </List>
          </CardContent>
        </Card>
      )}

      {data.overdue.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6">Overdue</Typography>
            <List dense>
              {data.overdue.map((s) => (
                <SeriesRow key={s.merchant} s={s} note={`expected ${s.expected_next}`}
                  categories={categories} onCategorise={categorise} onDismiss={dismiss} />
              ))}
            </List>
          </CardContent>
        </Card>
      )}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6">Running</Typography>
          <Typography variant="caption" color="text.secondary">
            {data.series.length} series. Total paid is what left the account, not
            what it costs now.
          </Typography>
          <Divider sx={{ my: 1 }} />
          <List dense>
            {data.series.map((s) => (
              <SeriesRow key={s.merchant + s.amount_centre_minor} s={s}
                categories={categories} onCategorise={categorise} onDismiss={dismiss} />
            ))}
          </List>
          {data.series.length === 0 && (
            <Typography color="text.secondary">Nothing detected yet.</Typography>
          )}
        </CardContent>
      </Card>

      {data.lapsed.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6" color="text.secondary">Lapsed</Typography>
            <Typography variant="caption" color="text.secondary">
              Missed enough cycles to read as cancelled. Shown, but not alerted on.
            </Typography>
            <List dense>
              {data.lapsed.map((s) => (
                <SeriesRow key={s.merchant} s={s} note={`last seen ${s.last_seen}`}
                  categories={categories} onCategorise={categorise} onDismiss={dismiss} />
              ))}
            </List>
          </CardContent>
        </Card>
      )}
      {dismissed.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6" color="text.secondary">Not subscriptions</Typography>
            <Typography variant="caption" color="text.secondary">
              Detection is a guess, and these are the ones you said it got wrong.
              They stay out of the total until you put them back.
            </Typography>
            <List dense>
              {dismissed.map((d) => (
                <ListItem key={d.merchant_norm + d.amount_centre_minor} divider disableGutters
                  secondaryAction={
                    <Button size="small" startIcon={<UndoIcon />} onClick={() => restore(d)}>
                      Put back
                    </Button>
                  }
                >
                  <ListItemText
                    primary={d.merchant_norm}
                    secondary={`${magnitude(d.amount_centre_minor)} · dismissed ${d.dismissed_at.slice(0, 10)}`}
                  />
                </ListItem>
              ))}
            </List>
          </CardContent>
        </Card>
      )}
      {marked.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6" color="text.secondary">Marked by you</Typography>
            <Typography variant="caption" color="text.secondary">
              Things you said repeat, on a period you chose. Listed here because
              a mark whose payments were later renamed stops appearing above,
              and this is the only place left that says it still exists.
            </Typography>
            <List dense>
              {marked.map((m) => (
                <ListItem key={m.merchant_norm + m.amount_centre_minor} divider disableGutters
                  secondaryAction={
                    <Button size="small" startIcon={<UndoIcon />}
                      onClick={() => unmark(m)}>
                      Un-mark
                    </Button>
                  }
                >
                  <ListItemText
                    primary={m.merchant_norm}
                    secondary={`${magnitude(m.amount_centre_minor)} · ${m.period_label} · marked ${m.marked_at.slice(0, 10)}`}
                  />
                </ListItem>
              ))}
            </List>
          </CardContent>
        </Card>
      )}
    </Stack>
  );
}
