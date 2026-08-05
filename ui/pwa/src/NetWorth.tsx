/**
 * Net worth over time, with the arrow and percentage §5.1(D) asks for.
 *
 * The brief is explicit about the feeling this should produce: calm when money
 * is growing, *slight* concern when it is not — enough to prompt a look, not
 * enough to alarm. So the down state is `warning` rather than `error`. Red for
 * an ordinary month would make the signal worthless by the third time it fired.
 *
 * Where the percentage is undefined — a window that opened at zero or in debt —
 * nothing is shown in its place. A confident number here would be the one kind
 * of wrong this screen cannot afford.
 */
import { Box, Card, CardContent, Chip, Stack, Tooltip, Typography } from "@mui/material";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import TrendingFlatIcon from "@mui/icons-material/TrendingFlat";
import { Growth } from "./api";
import { money, percent } from "./money";

export default function NetWorth({ data }: { data: Growth }) {
  const { points, change } = data;
  const latest = points.at(-1);
  const movement = change.change_minor ?? 0;
  const rising = movement > 0;
  const flat = movement === 0;

  const tone = flat ? "default" : rising ? "success" : "warning";
  const Icon = flat ? TrendingFlatIcon : rising ? TrendingUpIcon : TrendingDownIcon;

  const highest = Math.max(...points.map((p) => p.total_minor), 1);
  const lowest = Math.min(...points.map((p) => p.total_minor), 0);
  const span = highest - lowest || 1;

  // A point is only as current as its stalest account, and an early one with
  // fewer accounts is not comparable with a later one.
  const partial = points.some((p) => p.accounts_known < (latest?.accounts_known ?? 0));

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
          <Box>
            <Typography variant="overline" color="text.secondary">Net worth</Typography>
            <Typography variant="h4" sx={{ fontVariantNumeric: "tabular-nums" }}>
              {money(latest?.total_minor)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {latest ? `as at ${latest.on}` : "no statements in range"}
            </Typography>
          </Box>
          <Stack alignItems="flex-end" spacing={0.5}>
            <Chip
              color={tone === "default" ? "default" : tone}
              icon={<Icon />}
              label={change.percent === null ? money(movement) : percent(change.percent)}
              sx={{ fontWeight: 600 }}
            />
            {change.percent !== null && (
              <Typography variant="caption" color="text.secondary">
                {money(movement)} over {data.window_months} months
              </Typography>
            )}
            {change.percent === null && change.change_minor !== null && (
              <Tooltip title="The window opened at zero or in debt, so a percentage would not mean anything.">
                <Typography variant="caption" color="text.secondary">
                  over {data.window_months} months
                </Typography>
              </Tooltip>
            )}
          </Stack>
        </Stack>

        <Stack direction="row" alignItems="flex-end" spacing={0.5}
          sx={{ height: 90, mt: 2 }}>
          {points.map((p) => (
            <Tooltip
              key={p.on}
              title={`${p.on} · ${money(p.total_minor)} · ${p.accounts_known} accounts reporting`}
              arrow
            >
              <Box
                sx={{
                  flex: 1, minWidth: 4,
                  height: `${Math.max(((p.total_minor - lowest) / span) * 100, 2)}%`,
                  bgcolor: rising ? "success.main" : "warning.main",
                  opacity: 0.8,
                  borderRadius: "2px 2px 0 0",
                }}
              />
            </Tooltip>
          ))}
        </Stack>

        {partial && (
          <Typography variant="caption" color="text.secondary">
            Earlier points cover fewer accounts, so the oldest part of this line
            is not comparable with the newest.
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}
