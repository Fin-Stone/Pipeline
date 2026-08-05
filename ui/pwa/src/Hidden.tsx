/**
 * Transactions taken out of the picture, and what they come to.
 *
 * The total is shown as prominently as the list. Hiding is legitimate — a
 * one-off house deposit is real spending and still not representative of
 * anything — but a dashboard that quietly omits things is worth less than one
 * that says what it omitted, so the size of the omission is never more than a
 * tab away.
 */
import { useEffect, useState } from "react";
import {
  Alert, Card, CardContent, IconButton, LinearProgress, List, ListItem,
  ListItemText, Stack, Tooltip, Typography,
} from "@mui/material";
import UndoIcon from "@mui/icons-material/Undo";
import { ApiError, Hidden as HiddenData, api } from "./api";
import { magnitude, money } from "./money";

export default function Hidden() {
  const [data, setData] = useState<HiddenData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api.hidden().then(setData)
      .catch((e) => setError(String(e instanceof ApiError ? e.detail : e)));

  useEffect(() => { load(); }, []);

  async function restore(id: number) {
    await api.unhide(id);
    load();
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!data) return <LinearProgress />;

  return (
    <Stack spacing={2}>
      <Card variant="outlined">
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            Hidden from every figure
          </Typography>
          <Typography variant="h4" sx={{ fontVariantNumeric: "tabular-nums" }}>
            {magnitude(data.total_minor)}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {data.count} transaction{data.count === 1 ? "" : "s"} excluded from
            totals, averages and trends. Nothing about them has changed in the
            ledger — restoring one puts it straight back.
          </Typography>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          {data.count === 0 ? (
            <Typography color="text.secondary">
              Nothing is hidden. Figures on the dashboard cover everything in range.
            </Typography>
          ) : (
            <List dense>
              {data.hidden.map((row) => (
                <ListItem
                  key={row.id} divider disableGutters
                  secondaryAction={
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography sx={{ fontVariantNumeric: "tabular-nums" }}>
                        {money(row.amount_minor)}
                      </Typography>
                      <Tooltip title="Show again">
                        <IconButton size="small" onClick={() => restore(row.id)}>
                          <UndoIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </Stack>
                  }
                >
                  <ListItemText
                    primary={row.counterparty_norm || "(no counterparty)"}
                    secondary={
                      `${row.posted_date} · ${row.institution}` +
                      (row.note ? ` · ${row.note}` : "")
                    }
                  />
                </ListItem>
              ))}
            </List>
          )}
        </CardContent>
      </Card>
    </Stack>
  );
}
