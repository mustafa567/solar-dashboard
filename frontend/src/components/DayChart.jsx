import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AXIS, CURSOR_STROKE, GRID_STROKE, SERIES } from "../lib/chartTheme.js";
import { ChartLegend, ChartTable, ChartTooltip } from "./ChartParts.jsx";
import { useResizeKey } from "../hooks/useResizeKey.js";

/*
  The day view: average kW per hour for the three channels on one axis (they are
  all kW, so a second axis would be a lie about scale).

  Marks are deliberately different shapes as well as different colours -- solar
  and grid import are filled areas, home usage is a line drawn over them -- so
  the series stay separable in greyscale and for colour-vision deficiency.

  Hours the poller never observed carry null rather than 0, and the line breaks
  there instead of drawing a dip that never happened.
*/

const CHANNELS = [
  { key: "solar", label: "Solar", color: SERIES.solar, mark: "area" },
  { key: "home", label: "Home usage", color: SERIES.home, mark: "line" },
  { key: "grid", label: "From grid", color: SERIES.grid, mark: "area" },
];

const TABLE_COLUMNS = [
  { key: "solar", label: "Solar" },
  { key: "home", label: "Home" },
  { key: "grid", label: "From grid" },
];

export default function DayChart({ points }) {
  const resizeKey = useResizeKey();
  const data = points.map((point) => ({
    t: point.t,
    label: point.label,
    // A bucket with no coverage is a gap, not a zero.
    solar: point.has_data ? point.solar_kw_avg : null,
    home: point.has_data ? point.home_kw_avg : null,
    grid: point.has_data ? point.grid_import_kw_avg : null,
  }));

  return (
    <div className="space-y-3">
      <div className="h-72 sm:h-80">
        <ResponsiveContainer key={resizeKey} width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 8, right: 6, bottom: 0, left: -18 }}
          >
            <defs>
              <linearGradient id="solarFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SERIES.solar} stopOpacity={0.55} />
                <stop offset="100%" stopColor={SERIES.solar} stopOpacity={0.04} />
              </linearGradient>
              <linearGradient id="gridFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SERIES.grid} stopOpacity={0.4} />
                <stop offset="100%" stopColor={SERIES.grid} stopOpacity={0.03} />
              </linearGradient>
            </defs>

            <CartesianGrid
              stroke={GRID_STROKE}
              strokeDasharray="2 4"
              vertical={false}
            />
            <XAxis
              dataKey="label"
              stroke={AXIS.stroke}
              tick={AXIS.tick}
              tickLine={false}
              interval="preserveStartEnd"
              minTickGap={28}
            />
            <YAxis
              stroke={AXIS.stroke}
              tick={AXIS.tick}
              tickLine={false}
              axisLine={false}
              width={52}
              label={undefined}
            />
            <Tooltip
              content={<ChartTooltip unit="kW" />}
              cursor={{ stroke: CURSOR_STROKE, strokeWidth: 1 }}
            />

            <Area
              type="monotone"
              dataKey="solar"
              name="Solar"
              stroke={SERIES.solar}
              strokeWidth={2}
              fill="url(#solarFill)"
              connectNulls={false}
              isAnimationActive={false}
              activeDot={{ r: 4, strokeWidth: 0 }}
            />
            <Area
              type="monotone"
              dataKey="grid"
              name="From grid"
              stroke={SERIES.grid}
              strokeWidth={2}
              fill="url(#gridFill)"
              connectNulls={false}
              isAnimationActive={false}
              activeDot={{ r: 4, strokeWidth: 0 }}
            />
            <Line
              type="monotone"
              dataKey="home"
              name="Home usage"
              stroke={SERIES.home}
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
              activeDot={{ r: 4, strokeWidth: 0 }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <ChartLegend channels={CHANNELS} unit="average kW per hour" />
      <ChartTable rows={data} unit="kW" columns={TABLE_COLUMNS} />
    </div>
  );
}
