import { formatKw } from "../lib/format.js";

/*
  The flow triangle: Solar at the apex, Home and Grid at the base, with current
  visibly travelling along whichever legs are actually carrying power. Dash
  speed is derived from the real kW, so a heavy draw looks fast and a trickle
  looks slow -- the diagram reads as an instrument, not an illustration.

  Geometry is fixed in a viewBox and scaled by CSS, so it is sharp on a phone
  and on a wall-mounted monitor without breakpoint juggling.
*/

const NODES = {
  solar: { x: 162, y: 56, r: 37 },
  home: { x: 74, y: 228, r: 42 },
  grid: { x: 250, y: 228, r: 37 },
};

// Below this a leg is drawn as dormant. Matches the backend's idle threshold.
const ACTIVE_KW = 0.02;

/** Trim a segment back to each circle's edge so lines meet nodes cleanly. */
function leg(fromKey, toKey) {
  const from = NODES[fromKey];
  const to = NODES[toKey];
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  const gap = 6;
  return {
    x1: from.x + ux * (from.r + gap),
    y1: from.y + uy * (from.r + gap),
    x2: to.x - ux * (to.r + gap),
    y2: to.y - uy * (to.r + gap),
  };
}

/** Faster dashes for more power. Clamped so it never strobes or stalls. */
function flowDuration(kw) {
  const seconds = 3.4 / Math.max(0.15, kw);
  return `${Math.min(5.5, Math.max(0.55, seconds)).toFixed(2)}s`;
}

function Leg({ fromKey, toKey, kw, color, label }) {
  const { x1, y1, x2, y2 } = leg(fromKey, toKey);
  const active = kw > ACTIVE_KW;
  return (
    <g>
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke={active ? color : "var(--color-hairline)"}
        strokeWidth={active ? 3 : 2}
        strokeLinecap="round"
        opacity={active ? 0.32 : 1}
      />
      {active && (
        <line
          className="flow-current"
          style={{ "--flow-duration": flowDuration(kw) }}
          x1={x1}
          y1={y1}
          x2={x2}
          y2={y2}
          stroke={color}
          strokeWidth={3.5}
        >
          <title>{`${label}: ${formatKw(kw)} kW`}</title>
        </line>
      )}
    </g>
  );
}

function SolarGlyph({ x, y, dimmed }) {
  const stroke = dimmed ? "var(--color-solar-dim)" : "var(--color-solar)";
  return (
    <g transform={`translate(${x} ${y})`} stroke={stroke} strokeWidth="2">
      <circle r="6.5" fill={dimmed ? "none" : "var(--color-solar)"} />
      {[0, 45, 90, 135, 180, 225, 270, 315].map((angle) => (
        <line
          key={angle}
          x1="0"
          y1="-10"
          x2="0"
          y2="-13.5"
          strokeLinecap="round"
          transform={`rotate(${angle})`}
        />
      ))}
    </g>
  );
}

function HomeGlyph({ x, y }) {
  return (
    <g
      transform={`translate(${x} ${y})`}
      stroke="var(--color-home-bright)"
      strokeWidth="2"
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M-11 0 L0 -10 L11 0" />
      <path d="M-8 -1 L-8 10 L8 10 L8 -1" />
    </g>
  );
}

function GridGlyph({ x, y, dimmed }) {
  const stroke = dimmed ? "var(--color-grid-dim)" : "var(--color-grid)";
  return (
    <g transform={`translate(${x} ${y})`}>
      {/* A transmission pylon: the grid's own vernacular. */}
      <g stroke={stroke} strokeWidth="2" fill="none" strokeLinecap="round">
        <path d="M-7 11 L-3 -10 L3 -10 L7 11" />
        <path d="M-9 -1 L9 -1" />
        <path d="M-11 -6 L11 -6" />
        <path d="M-5 5 L5 5" />
      </g>
    </g>
  );
}

function Node({ nodeKey, glyph, label, value, unit, accent, emphasis, note }) {
  const { x, y, r } = NODES[nodeKey];
  return (
    <g>
      <circle
        cx={x}
        cy={y}
        r={r}
        fill={emphasis ? "var(--color-panel-raised)" : "var(--color-panel)"}
        stroke={accent}
        strokeWidth={emphasis ? 2 : 1.5}
        opacity={emphasis ? 1 : 0.9}
      />
      {glyph}
      <text
        x={x}
        y={y + r + 20}
        textAnchor="middle"
        className="eyebrow"
        fill="var(--color-ink-faint)"
        style={{ fontSize: 9.5, letterSpacing: "0.14em" }}
      >
        {label}
      </text>
      <text
        x={x}
        y={y + r + 40}
        textAnchor="middle"
        className="readout"
        fill="var(--color-ink)"
        style={{ fontSize: 17, fontWeight: 600 }}
      >
        {value}
        <tspan
          fill="var(--color-ink-faint)"
          style={{ fontSize: 10.5, fontWeight: 500 }}
        >
          {` ${unit}`}
        </tspan>
      </text>
      {note && (
        <text
          x={x}
          y={y + r + 55}
          textAnchor="middle"
          fill="var(--color-ink-faint)"
          style={{ fontSize: 9.5 }}
        >
          {note}
        </text>
      )}
    </g>
  );
}

export default function FlowDiagram({ live }) {
  const flows = live?.flows ?? {
    solar_to_home_kw: 0,
    solar_to_grid_kw: 0,
    grid_to_home_kw: 0,
  };
  const solarKw = live?.solar_kw ?? 0;
  const homeKw = live?.home_kw ?? 0;
  const gridKw = Math.abs(live?.grid_kw ?? 0);
  const direction = live?.grid_direction;

  const solarDim = solarKw <= ACTIVE_KW;
  const gridDim = direction === "idle" || direction == null;

  const gridNote =
    direction === "import"
      ? "importing"
      : direction === "export"
        ? "exporting"
        : direction === "idle"
          ? "idle"
          : "";

  return (
    <svg
      viewBox="0 0 324 338"
      className="w-full max-w-sm mx-auto"
      role="img"
      aria-label={
        live?.has_data
          ? `Solar ${formatKw(solarKw)} kilowatts, home usage ${formatKw(
              homeKw,
            )} kilowatts, grid ${formatKw(gridKw)} kilowatts ${gridNote}`
          : "No readings yet"
      }
    >
      <Leg
        fromKey="solar"
        toKey="home"
        kw={flows.solar_to_home_kw}
        color="var(--color-solar)"
        label="Solar to home"
      />
      <Leg
        fromKey="solar"
        toKey="grid"
        kw={flows.solar_to_grid_kw}
        color="var(--color-solar)"
        label="Solar to grid"
      />
      <Leg
        fromKey="grid"
        toKey="home"
        kw={flows.grid_to_home_kw}
        color="var(--color-grid)"
        label="Grid to home"
      />

      <Node
        nodeKey="solar"
        label="Solar"
        value={formatKw(solarKw)}
        unit="kW"
        accent={solarDim ? "var(--color-solar-dim)" : "var(--color-solar)"}
        glyph={
          <SolarGlyph x={NODES.solar.x} y={NODES.solar.y} dimmed={solarDim} />
        }
      />
      <Node
        nodeKey="home"
        label="Home"
        value={formatKw(homeKw)}
        unit="kW"
        accent="var(--color-home)"
        emphasis
        glyph={<HomeGlyph x={NODES.home.x} y={NODES.home.y} />}
      />
      <Node
        nodeKey="grid"
        label="Grid"
        value={formatKw(gridKw)}
        unit="kW"
        accent={gridDim ? "var(--color-grid-dim)" : "var(--color-grid)"}
        note={gridNote}
        glyph={<GridGlyph x={NODES.grid.x} y={NODES.grid.y} dimmed={gridDim} />}
      />
    </svg>
  );
}
