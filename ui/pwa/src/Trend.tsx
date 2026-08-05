/**
 * Spending per period, as bars.
 *
 * Hand-drawn rather than pulled from a chart library: it is a column of
 * rectangles, and a charting dependency on a box serving a PWA over a LAN
 * costs more than it returns. It also keeps the bars honest — every one is
 * scaled from the same maximum, with no axis tricks.
 *
 * Bar width is chosen by the server from the range, not here: days at a
 * fortnight or less, weeks up to a year, months beyond.
 */
import { Box, Stack, Tooltip, Typography } from "@mui/material";
import { Trend as TrendData } from "./api";
import { magnitude } from "./money";

function label(period: string, bucket: TrendData["bucket"]): string {
  const d = new Date(period + "T00:00:00");
  if (bucket === "month") return d.toLocaleDateString("en-SG", { month: "short", year: "2-digit" });
  if (bucket === "week") return d.toLocaleDateString("en-SG", { day: "numeric", month: "short" });
  return d.toLocaleDateString("en-SG", { day: "numeric", month: "short" });
}

export default function Trend({ data }: { data: TrendData }) {
  if (!data.points.length) {
    return <Typography color="text.secondary">Nothing in this range.</Typography>;
  }

  const largest = Math.max(...data.points.map((p) => Math.abs(p.total_minor)), 1);
  // Enough labels to orient, not so many they collide on a phone.
  const every = Math.ceil(data.points.length / 8);

  return (
    <Box>
      <Stack
        direction="row" alignItems="flex-end" spacing={0.5}
        sx={{ height: 180, overflowX: "auto", pb: 1 }}
      >
        {data.points.map((point, i) => {
          const height = (Math.abs(point.total_minor) / largest) * 100;
          return (
            <Tooltip
              key={point.period}
              title={`${point.period} · ${magnitude(point.total_minor)} · ${point.rows} transactions`}
              arrow
            >
              <Stack
                alignItems="center" justifyContent="flex-end"
                sx={{ flex: "1 0 18px", minWidth: 18, height: "100%" }}
              >
                <Box
                  sx={{
                    width: "100%",
                    // A zero-spend period still shows a sliver, so a gap reads
                    // as "nothing spent" rather than as missing data.
                    height: `${Math.max(height, 1.5)}%`,
                    bgcolor: "primary.main",
                    borderRadius: "3px 3px 0 0",
                    transition: "height 160ms ease",
                    "&:hover": { bgcolor: "primary.dark" },
                  }}
                />
                <Typography
                  variant="caption" noWrap
                  sx={{ mt: 0.5, fontSize: 10, color: "text.secondary", height: 14 }}
                >
                  {i % every === 0 ? label(point.period, data.bucket) : ""}
                </Typography>
              </Stack>
            </Tooltip>
          );
        })}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        One bar per {data.bucket} · tallest is {magnitude(-largest)}
      </Typography>
    </Box>
  );
}
