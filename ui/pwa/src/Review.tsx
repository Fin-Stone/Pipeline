/**
 * The review queue, architecture §3.3 — and the decisions it has produced.
 *
 * Ranked by what deciding each entry is worth, not alphabetically — the
 * occurrence count and running total sit next to every name because that is
 * the difference between a chore and an obvious call.
 *
 * **Both halves are on this page on purpose.** A decision is stored as a rule
 * that outranks everything imported and settles a name for good, which is
 * exactly the kind of thing somebody wants to look at again a week later —
 * usually because the dashboard now says something surprising. Deciding used to
 * be a one-way door: the row greyed out, a rule appeared in the database, and no
 * screen anywhere could name it again. So the decisions made are listed here,
 * under the queue that made them, each with a way back.
 *
 * The queue itself is loaded once and not re-fetched after each decision. A
 * decided name drops out of it, and a list that reflowed under the cursor
 * between two clicks would make working down it an exercise in not misfiring.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, IconButton, LinearProgress,
  MenuItem, Select, Snackbar, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, TextField, Tooltip, Typography,
} from "@mui/material";
import UndoIcon from "@mui/icons-material/Undo";
import { ApiError, Category, CategoryRule, Review as ReviewData, api } from "./api";
import { magnitude } from "./money";

/** A thing that happened, and how to make it not have happened. */
interface Done { message: string; undo: () => Promise<unknown> }

export default function Review() {
  const [data, setData] = useState<ReviewData | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [decisions, setDecisions] = useState<CategoryRule[]>([]);
  const [pending, setPending] = useState<Record<string, string>>({});
  //: What this sitting has settled, so a row greys out where it stands. The
  //  server is the authority on what exists; this is only about not making the
  //  queue jump while somebody is working down it.
  const [decided, setDecided] = useState<Record<string, string>>({});
  const [q, setQ] = useState("");
  const [done, setDone] = useState<Done | null>(null);
  const [error, setError] = useState<string | null>(null);

  const report = (e: unknown) => setError(String(e instanceof ApiError ? e.detail : e));
  const forgetLocally = (counterparty: string) =>
    setDecided((d) => {
      const next = { ...d };
      delete next[counterparty];
      return next;
    });

  const loadDecisions = useCallback(
    () => api.rules("operator", q || undefined).then((d) => setDecisions(d.rules)).catch(report),
    [q],
  );

  useEffect(() => {
    Promise.all([api.review(50), api.categories()])
      .then(([r, c]) => { setData(r); setCategories(c.categories); })
      .catch(report);
  }, []);

  useEffect(() => { loadDecisions(); }, [loadDecisions]);

  /** Every mutation goes through here, so nothing can change what the rules
   *  say without also naming what it did and how to take it back. */
  async function act(run: () => Promise<unknown>, message: string, undo: () => Promise<unknown>) {
    try {
      await run();
      setDone({ message, undo });
      await loadDecisions();
    } catch (e) {
      report(e);
    }
  }

  // Annotated, because the two are each other's undo and TypeScript cannot
  // infer a return type through that cycle.
  const decide = (counterparty: string): Promise<void> => {
    const category = pending[counterparty];
    if (!category) return Promise.resolve();
    return act(
      async () => {
        await api.decide(counterparty, category);
        setDecided((d) => ({ ...d, [counterparty]: category }));
      },
      `${counterparty} → ${category}`,
      () => undecide(counterparty),
    );
  };

  const undecide = (counterparty: string): Promise<void> =>
    act(
      async () => {
        await api.undecide(counterparty);
        forgetLocally(counterparty);
      },
      `${counterparty} is undecided again`,
      () => decide(counterparty),
    );

  const forget = (rule: CategoryRule) =>
    act(
      async () => {
        await api.deleteRule(rule.id);
        if (rule.counterparty) forgetLocally(rule.counterparty);
      },
      `${rule.counterparty ?? rule.pattern} is undecided again`,
      // By pattern, not by name: what comes back is a new row with a new id,
      // and rebuilding a pattern from a name would not reproduce anything that
      // was a real expression.
      () => api.addRule(rule.pattern, rule.category, rule.weight, rule.note),
    );

  if (!data) return error ? <Alert severity="error">{error}</Alert> : <LinearProgress />;

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error" onClose={() => setError(null)}>{error}</Alert>}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="overline" color="text.secondary">Still to decide</Typography>
          <Typography variant="h4">{data.outstanding}</Typography>
          <Typography variant="body2" color="text.secondary">
            worth {magnitude(data.value_at_stake_minor)} — the entries below are
            the ones worth the most, in order.
          </Typography>
        </CardContent>
      </Card>

      <Alert severity="info">
        A decision is stored as a rule, so it settles every past and future
        transaction with that counterparty, and outranks anything imported.
        Decisions do not reach the dashboard until they are applied, and every
        one of them can be taken back below.
      </Alert>

      <Card variant="outlined">
        <Box sx={{ overflowX: "auto" }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Counterparty</TableCell>
                <TableCell align="right">Rows</TableCell>
                <TableCell align="right">Total</TableCell>
                <TableCell sx={{ minWidth: 200 }}>Category</TableCell>
                <TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {data.items.map((item) => {
                const settled = decided[item.counterparty];
                return (
                  <TableRow key={item.counterparty} hover
                    sx={{ opacity: settled ? 0.45 : 1 }}>
                    <TableCell sx={{ wordBreak: "break-word" }}>
                      {item.counterparty}
                    </TableCell>
                    <TableCell align="right">{item.occurrences}</TableCell>
                    <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                      {magnitude(item.total_minor)}
                    </TableCell>
                    <TableCell>
                      {settled ? (
                        <Chip size="small" color="success" label={settled} />
                      ) : (
                        <Select
                          size="small" fullWidth displayEmpty
                          value={pending[item.counterparty] ?? ""}
                          onChange={(e) =>
                            setPending((p) => ({ ...p, [item.counterparty]: e.target.value }))
                          }
                        >
                          <MenuItem value="" disabled>Choose…</MenuItem>
                          {categories.map((c) => (
                            <MenuItem key={c.id} value={c.name}>{c.name}</MenuItem>
                          ))}
                        </Select>
                      )}
                    </TableCell>
                    <TableCell>
                      {settled ? (
                        <Tooltip title="Undecide this counterparty">
                          <IconButton size="small" onClick={() => undecide(item.counterparty)}>
                            <UndoIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      ) : (
                        <Button size="small" disabled={!pending[item.counterparty]}
                          onClick={() => decide(item.counterparty)}>
                          Decide
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Box>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="flex-start"
            sx={{ mb: 1 }} spacing={2}>
            <Box>
              <Typography variant="h6">Decisions you have made</Typography>
              <Typography variant="caption" color="text.secondary" component="p">
                Only your own. The rules that shipped or were imported are not
                listed — they were nobody's decision, and offering to undo one
                would promise something the next import takes straight back.
              </Typography>
            </Box>
            <TextField size="small" label="Find one" value={q}
              onChange={(e) => setQ(e.target.value)} sx={{ minWidth: 180 }} />
          </Stack>

          {decisions.length === 0 ? (
            <Typography color="text.secondary" variant="body2">
              {q ? "No decision matches that." : "Nothing decided by hand yet."}
            </Typography>
          ) : (
            <Box sx={{ overflowX: "auto" }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Counterparty</TableCell>
                    <TableCell>Filed as</TableCell>
                    <TableCell align="right">Rows it covers</TableCell>
                    <TableCell />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {decisions.map((rule) => (
                    <TableRow key={rule.id} hover>
                      <TableCell sx={{ wordBreak: "break-word" }}>
                        {/* The plain name where there is one. Showing somebody
                            `^IKEA\-RESTAURANT$` is showing them the
                            implementation of their own decision. */}
                        {rule.counterparty ?? rule.pattern}
                      </TableCell>
                      <TableCell><Chip size="small" label={rule.category} /></TableCell>
                      <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                        {rule.transactions ?? "—"}
                      </TableCell>
                      <TableCell align="right">
                        <Tooltip title="Undo this decision">
                          <IconButton size="small" onClick={() => forget(rule)}>
                            <UndoIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* The list above covers noticing next week. This covers the misclick,
          which is a different failure and needs a different answer. */}
      <Snackbar
        open={!!done} autoHideDuration={6000} onClose={() => setDone(null)}
        message={done?.message ?? ""}
        action={
          <Button size="small" color="secondary" onClick={() => {
            const undo = done?.undo;
            setDone(null);
            undo?.();
          }}>
            Undo
          </Button>
        }
      />
    </Stack>
  );
}
