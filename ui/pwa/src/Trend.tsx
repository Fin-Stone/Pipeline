/**
 * Money per period, as bars, with a trailing average over them.
 *
 * One chart for spending, income and net — `measure` picks which of the three
 * the server already sent. Re-fetching per measure would risk two series
 * bucketed differently, which is the one way a chart can lie without any
 * number being wrong.
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
import { money } from "./money";

/** Height of the plot area. The bars, the average line and the zero baseline
 *  are all positioned against it, so it is one constant rather than three. */
const BARS = 170;

function label(period: string, bucket: TrendData["bucket"]): string {
  const d = new Date(period + "T00:00:00");
  if (bucket === "month") return d.toLocaleDateString("en-SG", { month: "short", year: "2-digit" });
  if (bucket === "week") return d.toLocaleDateString("en-SG", { day: "numeric", month: "short" });
  return d.toLocaleDateString("en-SG", { day: "numeric", month: "short" });
}

type Measure = "out_minor" | "in_minor" | "net_minor";

export default function Trend({ data, measure = "out_minor" }: {
  data: TrendData; measure?: Measure;
}) {
  if (!data.points.length) {
    return <Typography color="text.secondary">Nothing in this range.</Typography>;
  }

  const value = (p: TrendData["points"][number]) => p[measure];
  const rollingOf = (p: TrendData["points"][number]) => p.rolling[measure];

  const values = data.points.map(value);
  const high = Math.max(...values);
  const low = Math.min(...values);

  // Two different charts, chosen by the data rather than by the caller.
  //
  // Spending is always negative and income always positive, so a zero baseline
  // there would pin every bar to one edge and waste the height. Those get
  // magnitudes from the floor, with the sign carried by the label — bigger is
  // taller, which is what a bar chart is read as.
  //
  // Net straddles zero, and there the baseline is the whole point: a surplus
  // and a deficit drawn the same way up would make a good month and a bad one
  // look identical. So it gets a real zero line, up for surplus, down for not.
  const straddles = low < 0 && high > 0;
  const reach = straddles ? high - low || 1 : Math.max(Math.abs(high), Math.abs(low)) || 1;

  const place = (v: number) =>
    straddles ? ((v - low) / reach) * 100 : (Math.abs(v) / reach) * 100;
  const base = straddles ? place(0) : 0;

  const every = Math.ceil(data.points.length / 8);
  const settled = data.rolling_window;

  return (
    <Box>
      {/* The scroll lives outside everything that has to line up. Scrolling the
          bars alone would slide them out from under the average line. */}
      <Box sx={{ overflowX: "auto", pb: 0.5 }}>
        <Box sx={{ position: "relative", minWidth: data.points.length * 22 }}>
          {data.points.length > 1 && (
            /* Trailing, not centred: a centred line needs periods that have not
               happened, and the question is whether things are heading up now. */
            <svg
              viewBox={`0 0 ${data.points.length - 1} 100`}
              preserveAspectRatio="none"
              style={{
                position: "absolute", top: 0, left: 0, height: BARS,
                width: "100%", pointerEvents: "none", zIndex: 2,
              }}
            >
              <polyline
                fill="none" stroke="currentColor" strokeWidth={1.75}
                vectorEffect="non-scaling-stroke" opacity={0.9}
                points={data.points
                  .map((p, i) => `${i},${100 - place(rollingOf(p))}`)
                  .join(" ")}
              />
            </svg>
          )}

          {straddles && (
            <Box sx={{
              position: "absolute", left: 0, right: 0, zIndex: 1,
              bottom: `calc(${base}% * ${BARS}px / 100)`,
              borderTop: "1px solid", borderColor: "divider",
            }} />
          )}

          <Stack direction="row" alignItems="stretch" spacing={0.5} sx={{ height: BARS }}>
            {data.points.map((point) => {
              const v = value(point);
              const above = place(v) >= base;
              const top = Math.max(place(v), base);
              const bottom = Math.min(place(v), base);
              return (
                <Tooltip
                  key={point.period}
                  arrow
                  title={
                    `${point.period} · ${money(v)} · ${point.rows} transactions` +
                    ` · trailing ${money(rollingOf(point))}` +
                    (point.rolling_of < settled ? ` (over ${point.rolling_of} only)` : "")
                  }
                >
                  <Box sx={{ flex: "1 0 18px", minWidth: 18, position: "relative" }}>
                    <Box sx={{
                      position: "absolute", left: 0, right: 0,
                      bottom: `${bottom}%`,
                      // A period with nothing in it still shows a sliver, so a
                      // gap reads as "nothing happened" and not as missing data.
                      height: `${Math.max(top - bottom, 1.2)}%`,
                      bgcolor: v >= 0 ? "success.main" : "primary.main",
                      opacity: point.rolling_of < settled ? 0.5 : 1,
                      borderRadius: above ? "3px 3px 0 0" : "0 0 3px 3px",
                      transition: "height 160ms ease",
                    }} />
                  </Box>
                </Tooltip>
              );
            })}
          </Stack>

          <Stack direction="row" spacing={0.5} sx={{ mt: 0.5 }}>
            {data.points.map((point, i) => (
              <Typography
                key={point.period} variant="caption" noWrap
                sx={{ flex: "1 0 18px", minWidth: 18, fontSize: 10, color: "text.secondary" }}
              >
                {i % every === 0 ? label(point.period, data.bucket) : ""}
              </Typography>
            ))}
          </Stack>
        </Box>
      </Box>

      <Typography variant="caption" color="text.secondary" display="block">
        One bar per {data.bucket} · the line is a {settled}-{data.bucket} trailing
        average
        {data.points.some((p) => p.rolling_of < settled)
          ? " · faded bars have not filled that window yet"
          : ""}
      </Typography>
    </Box>
  );
}
