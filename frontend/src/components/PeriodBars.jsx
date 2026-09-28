import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AXIS, GRID_STROKE, SERIES } from "../lib/chartTheme.js";
import { ChartLegend, ChartTable, ChartTooltip } from "./ChartParts.jsx";
import { useResizeKey } from "../hooks/useResizeKey.js";

/*
  Week / month / year: kWh totals per day or per month.

  Grouped rather than stacked, because solar generated and energy consumed are
  independent measures of the same period -- stacking them would invent a total
  that means nothing. One axis, since all three series are kWh.

  A month of three grouped bars is wider than a phone, so the plot scrolls inside
  its own container rather than squeezing 90 bars into 360px.
*/

const CHANNELS = [
  { key: "solar", label: "Solar generated", color: SERIES.solar, mark: "area" },
  { key: "home", label: "Home used", color: SERIES.home, mark: "area" },
  { key: "grid", label: "From grid", color: SERIES.grid, mark: "area" },
];

const TABLE_COLUMNS = [
  { key: "solar", label: "Solar" },
  { key: "home", label: "Home" },
  { key: "grid", label: "From grid" },
];

/** Enough width that three grouped bars per category stay readable.
 *
 * Kept low deliberately: a full month is 31 categories, and anything larger
 * forces a horizontal scrollbar on a desktop that has room to spare. The
 * container only scrolls once the viewport really is narrower than this.
 */
const MIN_WIDTH_PER_CATEGORY = 20;

export default function PeriodBars({ points }) {
  const resizeKey = useResizeKey();
  const data = points.map((point) => ({
    t: point.t,
    label: point.label,
    solar: point.solar_kwh,
    home: point.home_kwh,
    grid: point.grid_import_kwh,
  }));

  const minWidth = Math.max(280, data.length * MIN_WIDTH_PER_CATEGORY);

  return (
    <div className="space-y-3">
      <div className="scroll-subtle -mx-1 overflow-x-auto px-1">
        <div className="h-72 sm:h-80 w-full" style={{ minWidth }}>
          <ResponsiveContainer key={resizeKey} width="100%" height="100%">
            <BarChart
              data={data}
              margin={{ top: 8, right: 6, bottom: 0, left: -18 }}
              barGap={2}
              barCategoryGap="22%"
            >
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
                minTickGap={4}
              />
              <YAxis
                stroke={AXIS.stroke}
                tick={AXIS.tick}
                tickLine={false}
                axisLine={false}
                width={52}
              />
              <Tooltip
                content={<ChartTooltip unit="kWh" />}
                cursor={{ fill: "var(--color-panel-raised)", fillOpacity: 0.7 }}
              />

              <Bar
                dataKey="solar"
                name="Solar generated"
                fill={SERIES.solar}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
              <Bar
                dataKey="home"
                name="Home used"
                fill={SERIES.home}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
              <Bar
                dataKey="grid"
                name="From grid"
                fill={SERIES.grid}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <ChartLegend channels={CHANNELS} unit="kWh" />
      <ChartTable rows={data} unit="kWh" columns={TABLE_COLUMNS} />
    </div>
  );
}
