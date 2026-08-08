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
  Alert, Card, CardContent, Chip, Divider, FormControl, LinearProgress, List,
  ListItem, ListItemText, MenuItem, Select, Stack, Tooltip, Typography,
} from "@mui/material";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import { ApiError, Category, Recurring as RecurringData, Series, api } from "./api";
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
function SeriesRow({ s, note, categories, onCategorise }: {
  s: Series;
  note?: string;
  categories: Category[];
  onCategorise: (merchant: string, category: string) => Promise<void>;
}) {
  const change = s.price_changes.at(-1);
  const cheaper = change ? change.to_minor < change.from_minor : false;
  const [saving, setSaving] = useState(false);

  return (
    <ListItem divider disableGutters
      secondaryAction={
        <Stack alignItems="flex-end">
          <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
            {magnitude(s.amount_centre_minor)}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {magnitude(s.monthly_equivalent_minor)}/mo
          </Typography>
        </Stack>
      }
    >
      <ListItemText
        primary={
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            <Typography component="span">{s.merchant}</Typography>
            <Chip size="small" variant="outlined" label={s.period_label} />
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
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([api.recurring(), api.categories()])
      .then(([r, c]) => { setData(r); setCategories(c.categories); })
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
                  categories={categories} onCategorise={categorise} />
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
                  categories={categories} onCategorise={categorise} />
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
                categories={categories} onCategorise={categorise} />
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
                  categories={categories} onCategorise={categorise} />
              ))}
            </List>
          </CardContent>
        </Card>
      )}
    </Stack>
  );
}
