import type { Grade, TerrainAxis, TerrainUnavailable } from './types';

/** Each edit invalidates the previous request, including transports that ignore abort. */
export function createLatestRequest() {
  let version = 0;
  let controller: AbortController | null = null;
  function invalidate() {
    version += 1;
    controller?.abort();
    controller = null;
  }
  return {
    invalidate,
    start() {
      invalidate();
      controller = new AbortController();
      const currentVersion = version;
      const signal = controller.signal;
      return { signal, isCurrent: () => version === currentVersion && !signal.aborted };
    },
  };
}

export const terrainConnectionFailure: TerrainUnavailable = {
  status: 'unavailable', source: 'USGS 3DEP', reason: 'service_unavailable',
  message: 'We couldn’t check USGS terrain right now. You can continue with your own observations or retry.',
};

export function terrainUnavailableText(value: TerrainUnavailable): { title: string; detail: string } {
  switch (value.reason) {
    case 'no_coverage':
      return { title: 'No high-resolution USGS slope estimate for this site', detail: 'USGS 1 m terrain data is unavailable here. Use your own observations for the questions below.' };
    case 'unsupported_source':
      return { title: 'High-resolution terrain source could not be verified', detail: 'The source resolution or provenance could not be confirmed, so no estimate is shown. Use your own observations.' };
    case 'incomplete_coverage':
      return { title: 'Not enough terrain data for this placement', detail: 'One verified USGS 1 m source could not provide all four samples. No combined estimate is shown; use your own observations.' };
    default:
      return { title: 'USGS terrain could not be checked', detail: 'This does not establish whether high-resolution data covers the site. You can continue with your observations or retry.' };
  }
}

export type AxisView = { tone: 'low' | 'review'; percent: string; description: string; direction: string; baseline: string } | null;

export function terrainAxisView(axis: Partial<TerrainAxis> | null | undefined, orientation: 'along' | 'across', threshold = 3): AxisView {
  if (typeof axis?.percent !== 'number' || !Number.isFinite(axis.percent)
    || typeof axis.baselineMeters !== 'number' || !Number.isFinite(axis.baselineMeters) || axis.baselineMeters <= 0
    || typeof axis.riseMeters !== 'number' || !Number.isFinite(axis.riseMeters)) return null;
  const amount = Math.abs(axis.percent);
  const reviewAt = Number.isFinite(threshold) && threshold > 0 ? threshold : 3;
  const tone = amount >= reviewAt ? 'review' : 'low';
  const direction = axis.percent === 0 ? 'No estimated rise'
    : orientation === 'along' ? axis.percent > 0 ? 'Uphill toward the container' : 'Downhill toward the container'
      : axis.percent > 0 ? 'Right side higher' : 'Left side higher';
  return {
    tone,
    percent: amount > 0 && amount < 0.1 ? '<0.1%' : tone === 'low' && Number(amount.toFixed(1)) >= reviewAt ? `<${reviewAt.toFixed(1)}%` : `${amount.toFixed(1)}%`,
    description: tone === 'review' ? 'Noticeable estimated slope' : 'Low estimated slope',
    direction,
    baseline: `${axis.baselineMeters.toFixed(1)} m`,
  };
}

export function terrainDate(value: string | null): string {
  if (!value) return 'not provided';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }).format(date) : value;
}

export function terrainSummaryLines(grade: Grade | null): string[] {
  if (!grade) return ['Terrain estimates: not captured for this request. Manual site answers are recorded above.'];
  const lines = [`Terrain status: ${grade.status}`, `Terrain source: ${grade.source}`, `Terrain note: ${grade.message}`];
  if (grade.status === 'unavailable') return [...lines, `Unavailable reason: ${grade.reason ?? 'not specified'}`];
  lines.push(`Terrain source name: ${grade.sourceName}`, `Source date: ${grade.sourceDate ?? 'not provided'}`, `Grid resolution: ${grade.resolutionMeters} m (not a vertical accuracy claim)`, `Estimated at: ${grade.estimatedAt}`);
  for (const [key, title] of [['along', 'Along truck toward container'], ['across', 'Across footprint, left to right']] as const) {
    const axis = grade[key];
    const view = terrainAxisView(axis, key, grade.thresholdPercent);
    lines.push(`${title}: ${view ? `${axis.percent.toFixed(2)}% signed; ${view.direction}; ${view.description}; rise ${axis.riseMeters.toFixed(3)} m over ${axis.baselineMeters.toFixed(2)} m` : 'estimate unavailable'}`);
  }
  lines.push(`Tentative visual review cue: ${grade.thresholdPercent}% absolute slope; not an equipment limit or delivery decision.`, `Method: four terrain samples, including points ${grade.lateralOffsetMeters} m beyond each side edge at the combined truck/container footprint midpoint.`);
  for (const [name, sample] of Object.entries(grade.samples)) lines.push(`Sample ${name}: ${sample.lat.toFixed(7)}, ${sample.lng.toFixed(7)}; elevation ${sample.elevationMeters.toFixed(3)} m`);
  lines.push('Four terrain samples do not establish separate support planes, grade breaks, curbs, overhead clearance or safe unloading. Confirm site conditions directly.');
  return lines;
}
