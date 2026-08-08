/**
 * Re-pairing transfers, and the window that decides what pairs.
 *
 * A transfer is recorded twice, once on each side, and counting either as
 * spending is wrong. Pairing them is a judgement about two rows, and this is
 * where that judgement is made visible and adjustable.
 *
 * **The preview is the feature.** "206 links" tells nobody whether to apply
 * anything; "9 new, 2 no longer found" is the entire decision, so the numbers
 * shown are the diff against what is already recorded and nothing is written
 * until the operator says so.
 *
 * The three windows are separate because the window should widen with the
 * strength of the evidence. Amount and date alone is the weakest thing two rows
 * can say — on a household ledger the same round number turns up constantly —
 * so it gets the tightest bound. One leg naming the other's account number is
 * near-proof. A deposit account paying a card is a transfer by construction.
 */
import { useEffect, useState } from "react";
import {
  Alert, AlertTitle, Box, Button, Card, CardContent, Chip, Divider, Grid,
  LinearProgress, Stack, TextField, Tooltip, Typography,
} from "@mui/material";
import { ApiError, Realignment, TransferWindow, Transfers, api } from "./api";
import { magnitude } from "./money";

const FIELDS: {
  key: keyof TransferWindow; label: string; help: string;
}[] = [
  {
    key: "max_days", label: "Amount and date",
    help: "The weakest evidence: two rows that agree on nothing but the "
        + "number and roughly when. Keep this tight — widen it and unrelated "
        + "spending starts pairing with unrelated income.",
  },
  {
    key: "named_days", label: "One names the other's account",
    help: "Near-proof. Some banks write the far account number into the row, "
        + "and when they do the dates barely matter.",
  },
  {
    key: "card_days", label: "A deposit pays a card",
    help: "A transfer by construction — the purchases the card made are "
        + "already counted as spending, so counting the payment too doubles "
        + "the bill. Card issuers post on their own cycle, so this can be wide.",
  },
  {
    key: "min_days", label: "Smallest gap allowed",
    help: "Zero by default, because most transfers land the same day. Raise it "
        + "only if same-day coincidences are being paired.",
  },
];

export default function TransferRules() {
  const [state, setState] = useState<Transfers | null>(null);
  const [draft, setDraft] = useState<TransferWindow | null>(null);
  const [seen, setSeen] = useState<Realignment | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const report = (e: unknown) => setError(String(e instanceof ApiError ? e.detail : e));

  const load = () =>
    api.transfers()
      .then((t) => { setState(t); setDraft((d) => d ?? t.window); })
      .catch(report);

  useEffect(() => { load(); }, []);

  async function run(apply: boolean, save: boolean) {
    if (!draft) return;
    setBusy(true); setError(null); setNote(null);
    try {
      const outcome = await api.rematchTransfers(draft, apply, save);
      setSeen(outcome);
      if (apply) {
        setNote(
          `${outcome.found} link${outcome.found === 1 ? "" : "s"} recorded` +
          (outcome.saved ? ", window saved" : ""),
        );
        await load();
      }
    } catch (e) { report(e); } finally { setBusy(false); }
  }

  if (!state || !draft) {
    return error ? <Alert severity="error">{error}</Alert> : <LinearProgress />;
  }

  const changed = FIELDS.some((f) => draft[f.key] !== state.window[f.key]);
  const atDefaults = FIELDS.every((f) => draft[f.key] === state.defaults[f.key]);

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error" onClose={() => setError(null)}>{error}</Alert>}

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Typography variant="h6">Paired transfers</Typography>
            <Typography variant="h5" sx={{ fontVariantNumeric: "tabular-nums" }}>
              {state.linked}
            </Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary">
            Money moving between your own accounts, so neither side counts as
            spending or income. {state.manual} of these you marked by hand — a
            re-run never touches those.
          </Typography>
        </CardContent>
      </Card>

      <Alert severity="info">
        <AlertTitle>This runs itself after every import</AlertTitle>
        A statement arriving can complete a pair that was waiting for it: a card
        payment whose other leg had not been imported yet. You only need this
        screen to change the rule below, or to check what it is doing.
      </Alert>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6">How far apart the two legs may be</Typography>
          <Typography variant="caption" color="text.secondary" component="p" sx={{ mb: 2 }}>
            In days. The stronger the evidence a pair is real, the further apart
            the two rows are allowed to be — one window for all three would mean
            either missing card payments or guessing at coincidences.
          </Typography>

          <Grid container spacing={2}>
            {FIELDS.map((field) => (
              <Grid item xs={12} sm={6} key={field.key}>
                <Tooltip title={field.help} placement="top">
                  <TextField
                    fullWidth size="small" type="number" label={field.label}
                    value={draft[field.key]}
                    inputProps={{ min: 0, max: 365 }}
                    helperText={
                      draft[field.key] === state.defaults[field.key]
                        ? "default"
                        : `default ${state.defaults[field.key]}`
                    }
                    onChange={(e) =>
                      setDraft({ ...draft, [field.key]: Math.max(0, Number(e.target.value) || 0) })
                    }
                  />
                </Tooltip>
              </Grid>
            ))}
          </Grid>

          <Stack direction="row" spacing={1} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>
            <Button variant="outlined" disabled={busy} onClick={() => run(false, false)}>
              See what would change
            </Button>
            <Button variant="contained" disabled={busy || !seen}
              onClick={() => run(true, changed)}>
              Apply{changed ? " and remember" : ""}
            </Button>
            {!atDefaults && (
              <Button disabled={busy} onClick={() => setDraft(state.defaults)}>
                Back to defaults
              </Button>
            )}
          </Stack>
          {changed && (
            <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>
              A saved window travels with the ledger — through a backup, onto the
              next box. How far apart two banks book a transfer is a fact about
              the banks, not about this machine.
            </Typography>
          )}
          {note && <Alert severity="success" sx={{ mt: 2 }}>{note}</Alert>}
        </CardContent>
      </Card>

      {seen && <Preview outcome={seen} />}
    </Stack>
  );
}

function Preview({ outcome }: { outcome: Realignment }) {
  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="overline" color="text.secondary">
          {outcome.applied ? "Recorded" : "Would change"}
        </Typography>

        <Stack direction="row" spacing={3} sx={{ my: 1 }} flexWrap="wrap" useFlexGap>
          <Figure value={outcome.added} label="new" colour="success.main" />
          {/* Named separately from `new`, never netted. This is what applying
              gives up, and one net number would hide it. */}
          <Figure value={outcome.removed} label="no longer found" colour="warning.main" />
          <Figure value={outcome.unchanged} label="unchanged" />
          <Figure value={outcome.found} label="total" />
        </Stack>

        <Typography variant="body2" color="text.secondary">
          {magnitude(outcome.value_minor)} kept out of spending, across{" "}
          {outcome.rows_excluded} rows.
        </Typography>

        <Divider sx={{ my: 1.5 }} />
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {Object.entries(outcome.by_evidence).map(([evidence, count]) => (
            <Chip key={evidence} size="small" variant="outlined"
              label={`${count} — ${evidence}`} />
          ))}
        </Stack>

        {outcome.removed > 0 && !outcome.applied && (
          <Alert severity="warning" sx={{ mt: 2 }}>
            <AlertTitle>{outcome.removed} currently-paired row
              {outcome.removed === 1 ? "" : "s"} would stop being a transfer</AlertTitle>
            Those rows go back to counting as spending or income. Narrowing a
            window does this; so does removing a document that held one leg.
          </Alert>
        )}

        {outcome.ambiguous.length > 0 && (
          <Alert severity="info" sx={{ mt: 2 }}>
            <AlertTitle>{outcome.ambiguous.length} refused as ambiguous</AlertTitle>
            Each fits more than one counterpart equally well. A wrong link
            silently removes real spending from your total — which is the whole
            failure this pass exists to prevent — so none of them is guessed at.
            Mark them by hand from the transactions list if you know which is
            which.
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

function Figure({ value, label, colour }: { value: number; label: string; colour?: string }) {
  return (
    <Box>
      <Typography variant="h5" sx={{ fontVariantNumeric: "tabular-nums", color: colour }}>
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  );
}
