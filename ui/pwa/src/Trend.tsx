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

  // The line sits at the median, not the mean: one renovation drags a mean
  // somewhere no ordinary period has been, while the median keeps describing a
  // typical one. The mean is reported underneath so the gap is visible.
  const median = data.centre.median_minor;
  const mean = data.centre.mean_minor;
  const medianPct = median ? (Math.abs(median) / largest) * 100 : null;
  const skewed =
    median !== null && mean !== null && Math.abs(mean - median) > Math.abs(median) * 0.25;

  return (
    <Box>
      <Box sx={{ position: "relative" }}>
        {medianPct !== null && (
          <Tooltip title={`Median ${data.bucket}: ${magnitude(median)}`} arrow>
            <Box sx={{
              position: "absolute", left: 0, right: 0, zIndex: 1,
              bottom: `calc(${medianPct}% * (180px - 20px) / 100 + 20px)`,
              borderTop: "1px dashed", borderColor: "text.disabled",
              pointerEvents: "auto",
            }} />
          </Tooltip>
        )}
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
      </Box>
      <Typography variant="caption" color="text.secondary" display="block">
        One bar per {data.bucket} · dashed line is the median,{" "}
        {magnitude(median)} · mean {magnitude(mean)}
      </Typography>
      {skewed && (
        <Typography variant="caption" color="warning.main" display="block">
          The mean sits well away from the median, so a few large one-offs are
          carrying the average. The median is the better description of a
          typical {data.bucket}.
        </Typography>
      )}
    </Box>
  );
}
