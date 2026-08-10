/**
 * Saying a charge repeats, when the detector cannot tell.
 *
 * The detector wants three occurrences and gaps that barely vary, which is the
 * right bar for a guess and leaves real commitments invisible: a yearly premium
 * has two rows after two years, a plan taken out last month has one. The person
 * paying it has known all along.
 *
 * The screen is built around one risk. A mark reaches every row with the same
 * merchant and a comparable amount — not the row that was clicked — and getting
 * that wrong shows up months later as a monthly total nobody can account for.
 * So the matches are listed before anything is written, with the real gaps
 * beside the period being chosen, which is how somebody notices they picked
 * monthly for something billed yearly.
 */
import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, List, ListItem, ListItemText,
  MenuItem, Stack, TextField, Typography,
} from "@mui/material";
import { ApiError, PERIODS, RecurringCandidates, Txn, api } from "./api";
import { money } from "./money";

export default function RecurringDialog({ charge, onClose, onMarked }: {
  charge: Txn;
  onClose: () => void;
  /** Reports what happened and how to take it back. Every action that moves a
   *  figure has to have one, and this one moves the monthly commitment. */
  onMarked: (message: string, undo: () => Promise<unknown>) => void;
}) {
  const [period, setPeriod] = useState<string>("monthly");
  const [found, setFound] = useState<RecurringCandidates | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setLoading(true);
    api.recurringCandidates(charge.id, period)
      .then(setFound)
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)))
      .finally(() => setLoading(false));
  }, [charge.id, period]);

  const gaps = found?.gap_days ?? [];
  const expected = found?.expected_gap_days ?? null;
  // Flagged, never blocked. Irregular billing is exactly what a mark is for —
  // a quarterly bill invoiced whenever the vendor remembers is still quarterly
  // — so this says what the dates look like and leaves the judgement alone.
  const looksWrong =
    expected !== null && gaps.length > 0 &&
    gaps.some((g) => g < expected / 2 || g > expected * 2);

  async function mark() {
    if (!found) return;
    setBusy(true); setError(null);
    try {
      await api.markRecurring(found.merchant, found.amount_centre_minor, period);
      onMarked(
        `${found.merchant} marked as ${period}`,
        () => api.unmarkRecurring(found.merchant, found.amount_centre_minor),
      );
      onClose();
    } catch (e) {
      setError(String(e instanceof ApiError ? e.detail : e));
    } finally { setBusy(false); }
  }

  return (
    <Dialog open fullWidth maxWidth="sm" onClose={onClose}>
      <DialogTitle sx={{ pb: 0.5 }}>
        How often does this repeat?
        <Typography variant="body2" color="text.secondary">
          {charge.counterparty_norm || "(no counterparty)"} · {charge.posted_date} ·{" "}
          {money(charge.amount_minor)}
        </Typography>
      </DialogTitle>

      <DialogContent>
        <TextField
          select size="small" label="Every" value={period} sx={{ mt: 1, minWidth: 200 }}
          onChange={(e) => setPeriod(e.target.value)}
        >
          {PERIODS.map((p) => (
            <MenuItem key={p} value={p}>{p}</MenuItem>
          ))}
        </TextField>

        {found && (
          <Typography variant="body2" sx={{ mt: 2 }}>
            {money(found.monthly_equivalent_minor)} a month
            {found.expected_next && ` · next expected ${found.expected_next}`}
          </Typography>
        )}

        <Divider sx={{ my: 1.5 }} />

        <Typography variant="caption" color="text.secondary">
          This marks the <strong>merchant</strong>, not this one charge. Every
          payment below will belong to the series.
        </Typography>

        {loading && <CircularProgress size={20} sx={{ display: "block", my: 2 }} />}

        {found && !loading && (
          <List dense sx={{ maxHeight: 260, overflowY: "auto" }}>
            {found.matches.map((m, i) => (
              <ListItem key={m.txn_id} divider>
                <ListItemText
                  primary={m.posted_date}
                  secondary={i > 0 ? `${gaps[i - 1]} days later` : "first"}
                />
                <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {money(m.amount_minor)}
                </Typography>
              </ListItem>
            ))}
          </List>
        )}

        {found && !loading && found.matches.length === 1 && (
          <Alert severity="info" sx={{ mt: 1 }}>
            Only this payment so far. That is a fine reason to mark it — nothing
            else can know a plan started last month — and the next one will join
            it.
          </Alert>
        )}

        {looksWrong && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            These land about {Math.round(gaps.reduce((a, b) => a + b, 0) / gaps.length)} days
            apart, and {period} expects {expected}. Marking it anyway is fine if
            you know the billing is irregular.
          </Alert>
        )}

        {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2, display: "block" }}>
        <Box sx={{ mb: 1.5 }}>
          <Chip
            size="small" variant="outlined"
            label={`${found?.matches.length ?? 0} payment${
              found?.matches.length === 1 ? "" : "s"} · ${money(
              found?.monthly_equivalent_minor ?? 0)} a month`}
          />
        </Box>
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="contained" onClick={mark}
            disabled={busy || loading || !found || found.matches.length === 0}
          >
            Mark as {period}
          </Button>
        </Stack>
      </DialogActions>
    </Dialog>
  );
}
