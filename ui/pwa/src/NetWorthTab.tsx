/**
 * Net worth on its own screen, architecture §5.1(D).
 *
 * It used to sit on top of the spending dashboard, where it was the first thing
 * seen and the first thing scrolled past. It answers a different question from
 * "what did we spend" — it is built from declared statement balances, not from
 * transactions — and pairing them invited reading one as the cause of the other.
 */
import { useEffect, useState } from "react";
import {
  Alert, AlertTitle, Card, CardContent, FormControl, InputLabel, LinearProgress,
  MenuItem, Select, Stack, Typography,
} from "@mui/material";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import { ApiError, Growth as GrowthData, api } from "./api";
import NetWorth from "./NetWorth";

export default function NetWorthTab() {
  const [months, setMonths] = useState(12);
  const [growth, setGrowth] = useState<GrowthData | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.growth(months)
      .then((g) => { setGrowth(g); setUnavailable(null); })
      .catch((e) => {
        setGrowth(null);
        setUnavailable(String(e instanceof ApiError ? e.detail : e));
      })
      .finally(() => setLoading(false));
  }, [months]);

  return (
    <Stack spacing={2}>
      <Card variant="outlined">
        <CardContent>
          <FormControl size="small" sx={{ minWidth: 150 }}>
            <InputLabel>Window</InputLabel>
            <Select label="Window" value={months}
              onChange={(e) => setMonths(Number(e.target.value))}>
              {[3, 6, 12, 24, 60].map((m) => (
                <MenuItem key={m} value={m}>{m} months</MenuItem>
              ))}
            </Select>
          </FormControl>
        </CardContent>
      </Card>

      {loading && <LinearProgress />}
      {growth && <NetWorth data={growth} />}
      {unavailable && (
        <Alert severity="info" icon={<TrendingDownIcon />}>
          <AlertTitle>Net worth is unavailable</AlertTitle>
          {unavailable}
        </Alert>
      )}

      <Typography variant="caption" color="text.secondary">
        Built from the closing balance each statement declares, not from the
        transactions — so it is only as current as the newest statement
        ingested, and it moves in steps rather than continuously. Card balances
        count against you.
      </Typography>
    </Stack>
  );
}
