import { CHANNELS } from "../lib/chartTheme.js";

/*
  Shared chart furniture. Both charts use the same legend and tooltip so the two
  views read as one instrument: the mark swatch carries identity, and every
  number stays in an ink colour rather than wearing its series colour.
*/

/** Swatch shaped like the mark it stands for, so identity is not colour-alone. */
function MarkSwatch({ color, mark }) {
  if (mark === "line") {
    return (
      <span
        aria-hidden="true"
        className="inline-block h-0.5 w-3.5 rounded-full"
        style={{ background: color }}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="inline-block h-2.5 w-3.5 rounded-sm"
      style={{ background: color, opacity: 0.55, border: `1px solid ${color}` }}
    />
  );
}

export function ChartLegend({ channels = CHANNELS, unit }) {
  return (
    <ul className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5">
      {channels.map((channel) => (
        <li key={channel.key} className="flex items-center gap-1.5">
          <MarkSwatch color={channel.color} mark={channel.mark} />
          <span className="text-xs text-ink-muted">{channel.label}</span>
        </li>
      ))}
      {unit && <li className="text-xs text-ink-faint">({unit})</li>}
    </ul>
  );
}

export function ChartTooltip({ active, payload, label, unit, heading }) {
  if (!active || !payload?.length) return null;

  return (
    <div className="rounded-xl border border-hairline bg-panel-raised/95 px-3 py-2 shadow-xl backdrop-blur-sm">
      <p className="eyebrow text-ink-faint" style={{ fontSize: "0.625rem" }}>
        {heading ? `${heading} ${label}` : label}
      </p>
      <ul className="mt-1.5 space-y-1">
        {payload.map((entry) => (
          <li
            key={entry.dataKey}
            className="flex items-center justify-between gap-4 text-xs"
          >
            <span className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="inline-block h-2 w-2 rounded-sm"
                style={{ background: entry.color ?? entry.stroke }}
              />
              <span className="text-ink-muted">{entry.name}</span>
            </span>
            <span className="readout text-ink">
              {entry.value == null ? "--" : Number(entry.value).toFixed(2)}
              <span className="ml-1 text-ink-faint">{unit}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * A plain-text table of the same series, collapsed by default.
 *
 * The chart is the primary reading, but a screen reader cannot read an SVG
 * area, so the numbers are always reachable.
 */
export function ChartTable({ rows, unit, columns }) {
  if (!rows?.length) return null;
  return (
    <details className="group">
      <summary className="cursor-pointer text-xs text-ink-faint hover:text-ink-muted">
        Show the numbers
      </summary>
      <div className="mt-3 max-h-64 overflow-auto rounded-xl border border-hairline">
        <table className="w-full text-left text-xs">
          <thead className="sticky top-0 bg-panel-raised">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium text-ink-muted">
                Period
              </th>
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className="px-3 py-2 text-right font-medium text-ink-muted"
                >
                  {column.label} ({unit})
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.t} className="border-t border-hairline/60">
                <th
                  scope="row"
                  className="px-3 py-1.5 font-normal text-ink-muted"
                >
                  {row.label}
                </th>
                {columns.map((column) => (
                  <td key={column.key} className="readout px-3 py-1.5 text-right text-ink">
                    {row[column.key] == null
                      ? "--"
                      : Number(row[column.key]).toFixed(2)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
