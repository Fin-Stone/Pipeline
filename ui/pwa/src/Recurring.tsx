/**
 * The recurring payments page, architecture §5.1(C).
 *
 * Four purposes, and the layout serves them in order: what the commitment
 * totals, what is coming, what did not arrive, and what a subscription's price
 * has done. Lapsed series are shown apart from overdue ones because a
 * cancelled subscription and a skipped payment want opposite reactions — one
 * should go quiet, the other should be raised.
 */
import { useEffect, useState } from "react";
import {
  Alert, Card, CardContent, Chip, Divider, LinearProgress, List, ListItem,
  ListItemText, Stack, Tooltip, Typography,
} from "@mui/material";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import { ApiError, Recurring as RecurringData, Series, api } from "./api";
import { magnitude } from "./money";

function SeriesRow({ s, note }: { s: Series; note?: string }) {
  const rise = s.price_changes.at(-1);
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
            {rise && (
              // A price rise is a fact about a subscription, not a new one, so
              // it is shown on the series rather than as a second entry.
              <Tooltip title={`Changed on ${rise.on}`}>
                <Chip size="small" color="warning" icon={<ArrowUpwardIcon />}
                  label={`${magnitude(rise.from_minor)} → ${magnitude(rise.to_minor)}`} />
              </Tooltip>
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
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.recurring().then(setData)
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));
  }, []);

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
            <List dense>{data.due_soon.map((s) => <SeriesRow key={s.merchant} s={s} />)}</List>
          </CardContent>
        </Card>
      )}

      {data.overdue.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6">Overdue</Typography>
            <List dense>
              {data.overdue.map((s) => (
                <SeriesRow key={s.merchant} s={s} note={`expected ${s.expected_next}`} />
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
            {data.series.map((s) => <SeriesRow key={s.merchant + s.amount_centre_minor} s={s} />)}
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
                <SeriesRow key={s.merchant} s={s} note={`last seen ${s.last_seen}`} />
              ))}
            </List>
          </CardContent>
        </Card>
      )}
    </Stack>
  );
}
