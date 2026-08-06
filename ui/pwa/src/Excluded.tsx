/**
 * Everything currently taken out of the figures, and the way back from each.
 *
 * **This page exists because every action has to be reversible.** Three
 * different decisions remove money from the dashboard — hiding a row, calling
 * one a transfer, and settling a charge against paybacks — and each of them is
 * a single click on a crowded list. Two of them used to leave no trace: the row
 * simply vanished, the totals moved, and nothing anywhere could name it again.
 *
 * So the rule is that anything which changes a figure appears here with an undo
 * beside it. The totals are shown as prominently as the lists for the same
 * reason: a dashboard that quietly omits things is worth less than one that
 * says what it omitted.
 */
import { useEffect, useState } from "react";
import {
  Alert, Card, CardContent, Chip, Divider, IconButton, LinearProgress, List,
  ListItem, ListItemText, Stack, Tooltip, Typography,
} from "@mui/material";
import UndoIcon from "@mui/icons-material/Undo";
import {
  ApiError, Hidden as HiddenData, MarkedTransfers, Payback, api,
} from "./api";
import { magnitude, money } from "./money";

function Section({ title, why, count, total, children }: {
  title: string; why: string; count: number; total: number;
  children: React.ReactNode;
}) {
  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="baseline">
          <Typography variant="h6">{title}</Typography>
          <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
            {magnitude(total)}
          </Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary">{why}</Typography>
        <Divider sx={{ my: 1 }} />
        {count === 0
          ? <Typography color="text.secondary" variant="body2">Nothing here.</Typography>
          : children}
      </CardContent>
    </Card>
  );
}

function Row({ primary, secondary, amount, onUndo, undoLabel }: {
  primary: string; secondary: string; amount: number;
  onUndo: () => void; undoLabel: string;
}) {
  return (
    <ListItem
      divider disableGutters
      secondaryAction={
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
            {money(amount)}
          </Typography>
          <Tooltip title={undoLabel}>
            <IconButton size="small" onClick={onUndo}><UndoIcon fontSize="small" /></IconButton>
          </Tooltip>
        </Stack>
      }
    >
      <ListItemText primary={primary || "(no counterparty)"} secondary={secondary} />
    </ListItem>
  );
}

export default function Excluded() {
  const [hidden, setHidden] = useState<HiddenData | null>(null);
  const [marked, setMarked] = useState<MarkedTransfers | null>(null);
  const [paybacks, setPaybacks] = useState<Payback[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    Promise.all([api.hidden(), api.markedTransfers(), api.paybacks()])
      .then(([h, m, p]) => { setHidden(h); setMarked(m); setPaybacks(p.paybacks); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));

  useEffect(() => { load(); }, []);

  async function undo(fn: Promise<unknown>) {
    try { await fn; await load(); }
    catch (e) { setError(String(e instanceof ApiError ? e.detail : e)); }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!hidden || !marked) return <LinearProgress />;

  const paidBack = paybacks.reduce((t, p) => t + p.amount_minor, 0);

  return (
    <Stack spacing={2}>
      <Alert severity="info">
        Nothing on this page has been changed in the ledger. Every one of these
        is a decision about how to <em>read</em> a transaction, and every one of
        them can be undone from here.
      </Alert>

      <Section
        title="Hidden" count={hidden.count} total={hidden.total_minor}
        why="Left out of totals, averages and trends. Restoring one puts it straight back."
      >
        <List dense>
          {hidden.hidden.map((row) => (
            <Row
              key={row.id} primary={row.counterparty_norm} amount={row.amount_minor}
              secondary={`${row.posted_date} · ${row.institution}${row.note ? ` · ${row.note}` : ""}`}
              undoLabel="Show again" onUndo={() => undo(api.unhide(row.id))}
            />
          ))}
        </List>
      </Section>

      <Section
        title="Marked as transfers" count={marked.count} total={marked.total_minor}
        why="Money you said moved between your own accounts, so it is not spending or income. Only your own marks appear here — the matcher's are rebuilt from the ledger each run."
      >
        <List dense>
          {marked.marked.map((row) => (
            <Row
              key={row.id} primary={row.counterparty_norm} amount={row.txn_amount_minor}
              secondary={`${row.posted_date} · ${row.institution} · ${row.evidence}`}
              undoLabel="Not a transfer after all"
              onUndo={() => undo(api.unmarkTransfer(row.out_txn_id))}
            />
          ))}
        </List>
      </Section>

      <Section
        title="Paid back" count={paybacks.length} total={paidBack}
        why="Money that came back for a specific charge, so it is not income and the charge is smaller than the statement says. Undoing one restores both sides."
      >
        <List dense>
          {paybacks.map((row) => (
            <Row
              key={row.id} primary={row.counterparty_norm} amount={row.amount_minor}
              secondary={`${row.posted_date} · ${row.institution} · settles charge #${row.expense_txn_id}`}
              undoLabel="Unlink from that charge"
              onUndo={() => undo(api.unlinkPayback(row.expense_txn_id, row.income_txn_id))}
            />
          ))}
        </List>
      </Section>

      {hidden.count + marked.count + paybacks.length === 0 && (
        <Chip label="Every transaction in range is being counted" variant="outlined" />
      )}
    </Stack>
  );
}
