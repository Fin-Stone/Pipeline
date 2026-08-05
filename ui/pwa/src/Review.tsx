/**
 * The review queue, architecture §3.3.
 *
 * Ranked by what deciding each entry is worth, not alphabetically — the
 * occurrence count and running total sit next to every name because that is
 * the difference between a chore and an obvious call.
 *
 * A decision is permanent and outranks every imported rule, so the screen says
 * so rather than letting it feel like a guess being logged.
 */
import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, LinearProgress, MenuItem,
  Select, Snackbar, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  Typography,
} from "@mui/material";
import { ApiError, Category, Review as ReviewData, api } from "./api";
import { magnitude } from "./money";

export default function Review() {
  const [data, setData] = useState<ReviewData | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [pending, setPending] = useState<Record<string, string>>({});
  const [decided, setDecided] = useState<Set<string>>(new Set());
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    Promise.all([api.review(50), api.categories()])
      .then(([r, c]) => { setData(r); setCategories(c.categories); })
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));

  useEffect(() => { load(); }, []);

  async function decide(counterparty: string) {
    const category = pending[counterparty];
    if (!category) return;
    try {
      await api.decide(counterparty, category);
      setDecided((s) => new Set(s).add(counterparty));
      setNote(`${counterparty} → ${category}`);
    } catch (e) {
      setError(String(e instanceof ApiError ? e.detail : e));
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!data) return <LinearProgress />;

  return (
    <Stack spacing={2}>
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
        Decisions do not reach the dashboard until they are applied.
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
                const done = decided.has(item.counterparty);
                return (
                  <TableRow key={item.counterparty} hover
                    sx={{ opacity: done ? 0.45 : 1 }}>
                    <TableCell sx={{ wordBreak: "break-word" }}>
                      {item.counterparty}
                    </TableCell>
                    <TableCell align="right">{item.occurrences}</TableCell>
                    <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                      {magnitude(item.total_minor)}
                    </TableCell>
                    <TableCell>
                      {done ? (
                        <Chip size="small" color="success"
                          label={pending[item.counterparty]} />
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
                      <Button size="small" disabled={done || !pending[item.counterparty]}
                        onClick={() => decide(item.counterparty)}>
                        Decide
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Box>
      </Card>

      <Snackbar open={!!note} autoHideDuration={2500} onClose={() => setNote(null)}
        message={note ?? ""} />
    </Stack>
  );
}
