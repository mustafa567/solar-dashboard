/*
  Chart series colours.

  Both themes carry their own validated triple; the values live in index.css as
  --series-* and are referenced here as CSS variables so a theme switch repaints
  the charts with no JavaScript involved.

  Each triple was snapped onto its mode's lightness band (OKLCH L 0.43-0.77
  light, 0.48-0.67 dark; chroma >= 0.10) and checked with the dataviz validator
  for all-pairs colour-vision separation:

    night  #CE7E00 #00A597 #917FC8  on #0F1829 -- worst pair dE 8.2 deutan
    day    #B86E00 #008A7E #7A66B8  on #FFFFFF -- worst pair dE 10.0 deutan

  Both pass the lightness band, chroma floor, CVD separation, normal-vision
  floor and contrast checks. The dark set is a separate selection, not an
  automatic flip of the light one.

  The brief asks for a neutral for home usage. A true neutral fails twice: no
  chroma (reads grey) and it collides with the teal for deuteranopia (measured
  dE 3.9 -- indistinguishable). Home is therefore the least-saturated slot that
  passes: a cool slate-violet. Series are additionally separated by mark type
  (area / area / line), never by colour alone.

  If you change any of these, re-run the validator rather than eyeballing it.
*/
export const SERIES = {
  solar: "var(--series-solar)",
  grid: "var(--series-grid)",
  home: "var(--series-home)",
};

/*
  Chart animation is off everywhere. This view re-polls while it is open, and
  animating the series on every refresh makes the chart lurch instead of simply
  updating. It also removes a class of confusing half-drawn frames.
*/
export const ANIMATE_CHARTS = false;

export const AXIS = {
  stroke: "var(--chart-axis)",
  tick: {
    fill: "var(--chart-tick)",
    fontSize: 11,
    fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
  },
};

export const GRID_STROKE = "var(--chart-grid-line)";
export const CURSOR_STROKE = "var(--chart-cursor)";

/** The three channels, in fixed order. Never cycled, never reordered. */
export const CHANNELS = [
  { key: "solar", label: "Solar", color: SERIES.solar, mark: "area" },
  { key: "home", label: "Home usage", color: SERIES.home, mark: "line" },
  { key: "grid", label: "From grid", color: SERIES.grid, mark: "area" },
];
