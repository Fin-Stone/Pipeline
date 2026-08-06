/**
 * Linking the money that came back for one shared charge.
 *
 * A dialog rather than a trip to the Income tab: the operator is in the middle
 * of reading their spending, and sending them somewhere else costs the filters
 * and the scroll position they got there with.
 *
 * The running total is the point of the screen. Ticking four names and seeing
 * "this charge becomes -$60.00" is the whole decision, and it is shown before
 * anything is written rather than discovered on the dashboard afterwards.
 */
import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Divider, FormControlLabel, List, ListItem,
  ListItemButton, ListItemText, Stack, TextField, Typography,
} from "@mui/material";
import { ApiError, Candidate, Payback, Txn, api } from "./api";
import { money } from "./money";

export default function PaybackDialog({ charge, onClose, onLinked }: {
  charge: Txn;
  onClose: () => void;
  onLinked: () => void;
}) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [linked, setLinked] = useState<Payback[]>([]);
  const [picked, setPicked] = useState<number[]>([]);
  const [q, setQ] = useState("");
  const [nearby, setNearby] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setLoading(true);
    // Both, because "what is already linked" and "what could be" answer
    // different halves of the same question.
    Promise.all([
      api.paybackCandidates(charge.id, q || undefined, nearby ? 30 : undefined),
      api.paybacks(charge.id),
    ])
      .then(([c, p]) => { setCandidates(c.candidates); setLinked(p.paybacks); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)))
      .finally(() => setLoading(false));
  }, [charge.id, q, nearby]);

  // Summed in minor units and formatted once, never by adding formatted values.
  const selected = candidates
    .filter((c) => picked.includes(c.id))
    .reduce((total, c) => total + c.amount_minor, 0);
  const already = linked.reduce((total, p) => total + p.amount_minor, 0);
  const spent = -charge.amount_minor;
  const becomes = charge.amount_minor + already + selected;
  // A charge cannot become income; the server refuses it, so the button says so
  // first rather than letting the operator find out from an error.
  const tooMuch = already + selected > spent;

  async function link() {
    setBusy(true); setError(null);
    try {
      await api.linkPaybacks(charge.id, picked);
      onLinked();
      onClose();
    } catch (e) {
      setError(String(e instanceof ApiError ? e.detail : e));
    } finally { setBusy(false); }
  }

  async function unlinkAll() {
    setBusy(true); setError(null);
    try {
      await api.unlinkPaybacks(charge.id);
      onLinked();
      onClose();
    } catch (e) {
      setError(String(e instanceof ApiError ? e.detail : e));
    } finally { setBusy(false); }
  }

  return (
    <Dialog open fullWidth maxWidth="sm" onClose={onClose}>
      <DialogTitle sx={{ pb: 0.5 }}>
        Who paid you back?
        <Typography variant="body2" color="text.secondary">
          {charge.counterparty_norm || "(no counterparty)"} · {charge.posted_date} ·{" "}
          {money(charge.amount_minor)}
        </Typography>
      </DialogTitle>

      <DialogContent>
        {linked.length > 0 && (
          <Alert severity="info" sx={{ mb: 2 }}
            action={<Button size="small" disabled={busy} onClick={unlinkAll}>Unlink all</Button>}>
            {money(already)} already linked from {linked.length} payment
            {linked.length === 1 ? "" : "s"}.
          </Alert>
        )}

        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <TextField
            size="small" fullWidth label="Search the money coming in"
            value={q} onChange={(e) => setQ(e.target.value)}
          />
          <FormControlLabel
            control={<Checkbox checked={nearby} onChange={(e) => setNearby(e.target.checked)} />}
            label="± 30 days"
            sx={{ whiteSpace: "nowrap" }}
          />
        </Stack>

        <Typography variant="caption" color="text.secondary">
          Nearest the charge first. Anything already settling another charge, or
          marked as a transfer, is not offered — linking it would be refused.
        </Typography>
        <Divider sx={{ my: 1 }} />

        {loading && <CircularProgress size={20} />}
        <List dense sx={{ maxHeight: 300, overflowY: "auto" }}>
          {candidates.map((c) => (
            <ListItem key={c.id} disablePadding divider>
              <ListItemButton
                onClick={() => setPicked((p) =>
                  p.includes(c.id) ? p.filter((x) => x !== c.id) : [...p, c.id])}
              >
                <Checkbox edge="start" checked={picked.includes(c.id)} tabIndex={-1} />
                <ListItemText
                  primary={c.counterparty_norm || c.description_raw}
                  secondary={`${c.posted_date} · ${c.institution}`}
                />
                <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {money(c.amount_minor)}
                </Typography>
              </ListItemButton>
            </ListItem>
          ))}
          {!loading && candidates.length === 0 && (
            <Typography color="text.secondary" sx={{ py: 2 }}>
              Nothing came in that could be a payback for this.
              {nearby && " Try widening the window."}
            </Typography>
          )}
        </List>

        {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2, display: "block" }}>
        <Box sx={{ mb: 1.5 }}>
          <Typography variant="body2" color={tooMuch ? "error" : "text.secondary"}>
            {money(already + selected)} back against {money(spent)} spent
          </Typography>
          <Typography variant="h6" sx={{ fontVariantNumeric: "tabular-nums" }}>
            this charge becomes {money(becomes)}
          </Typography>
          {tooMuch && (
            <Typography variant="caption" color="error">
              That is more than the charge. A charge cannot become income — one of
              these belongs to something else.
            </Typography>
          )}
          {!tooMuch && becomes === 0 && picked.length > 0 && (
            <Chip size="small" color="success" variant="outlined" sx={{ mt: 0.5 }}
              label="Fully paid back — you fronted this and got it all returned" />
          )}
        </Box>
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="contained" onClick={link}
            disabled={busy || tooMuch || picked.length === 0}
          >
            Link {picked.length || ""}
          </Button>
        </Stack>
      </DialogActions>
    </Dialog>
  );
}
