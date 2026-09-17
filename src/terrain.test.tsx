import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CoverageStatus, GradeAdvisory, InlineAdvisory } from './TerrainAdvisory';
import { createLatestRequest, terrainAxisView, terrainSummaryLines, terrainUnavailableText } from './terrain';
import type { Grade, TerrainAxis } from './types';

const axis = (percent: number): TerrainAxis => ({ percent, baselineMeters: 20, riseMeters: percent / 5, classification: Math.abs(percent) >= 3 ? 'review' : 'low', direction: percent < 0 ? 'downhill' : percent > 0 ? 'uphill' : 'level' });
const available: Grade = {
  status: 'available', source: 'USGS 3DEP', message: 'Four-point terrain estimate.',
  resolutionMeters: 1, sourceName: 'USGS 1 Meter DEM Example', sourceDate: '2024-04-01',
  thresholdPercent: 3, lateralOffsetMeters: 1.5, estimatedAt: '2026-09-17T00:00:00Z',
  along: axis(1), across: { ...axis(-4), baselineMeters: 5.4384, riseMeters: -0.217536, direction: 'left_up' },
  samples: {
    containerEnd: { lat: 47.6, lng: -122.3, elevationMeters: 100.2 },
    truckFront: { lat: 47.5998, lng: -122.3, elevationMeters: 100 },
    left: { lat: 47.5999, lng: -122.30002, elevationMeters: 100.12 },
    right: { lat: 47.5999, lng: -122.29998, elevationMeters: 100 },
  },
  evidenceToken: 'server-signed-evidence',
};

describe('terrain advisory interpretation', () => {
  it('keeps unavailable and nonfinite data distinct from a measured zero', () => {
    expect(terrainAxisView(undefined, 'along')).toBeNull();
    expect(terrainAxisView({ ...axis(0), percent: undefined }, 'along')).toBeNull();
    expect(terrainAxisView(axis(NaN), 'along')).toBeNull();
    expect(terrainAxisView(axis(Infinity), 'along')).toBeNull();
    expect(terrainAxisView({ ...axis(0), baselineMeters: 0 }, 'along')).toBeNull();
    expect(terrainAxisView(axis(0), 'along')).toMatchObject({ percent: '0.0%', tone: 'low', direction: 'No estimated rise' });
  });
  it('uses absolute slope for the tentative 3% cue and sign for each direction', () => {
    expect(terrainAxisView(axis(2.99), 'along')?.tone).toBe('low');
    expect(terrainAxisView(axis(2.99), 'along')?.percent).toBe('<3.0%');
    expect(terrainAxisView(axis(3), 'along')).toMatchObject({ tone: 'review', direction: 'Uphill toward the container' });
    expect(terrainAxisView(axis(-3), 'along')).toMatchObject({ tone: 'review', direction: 'Downhill toward the container' });
    expect(terrainAxisView(axis(4), 'across')?.direction).toBe('Right side higher');
    expect(terrainAxisView(axis(-4), 'across')?.direction).toBe('Left side higher');
    expect(terrainAxisView(axis(0.001), 'along')?.percent).toBe('<0.1%');
  });
  it('does not render a low-slope result while loading, even if old values remain', () => {
    const html = renderToStaticMarkup(<GradeAdvisory grade={available} loading onRefresh={() => {}} />);
    expect(html).toContain('Checking four USGS terrain samples');
    expect(html).not.toContain('Low estimated slope');
    expect(html).not.toContain('terrain-axis-value');
  });
  it('renders both axes, source, date and limitations without selecting any answer', () => {
    const html = renderToStaticMarkup(<GradeAdvisory grade={available} loading={false} onRefresh={() => {}} />);
    expect(html).toContain('Low estimated slope');
    expect(html).toContain('Noticeable estimated slope');
    expect(html).toContain('Left side higher');
    expect(html).toContain('USGS 1 Meter DEM Example');
    expect(html).toContain('Apr 1, 2024');
    expect(html).toContain('not an equipment limit');
    expect(html).not.toContain('type="radio"');
    expect(html).not.toContain('checked=');
  });
  it('keeps service failure separate from lack of coverage', () => {
    const noCoverage = terrainUnavailableText({ status: 'unavailable', source: 'USGS 3DEP', reason: 'no_coverage', message: 'Missing' });
    const failure = terrainUnavailableText({ status: 'unavailable', source: 'USGS 3DEP', reason: 'service_unavailable', message: 'Timed out' });
    expect(noCoverage.title).toContain('No high-resolution');
    expect(failure.title).toContain('could not be checked');
    expect(failure.detail).toContain('does not establish');
    const html = renderToStaticMarkup(<CoverageStatus coverage={{ status: 'unavailable', source: 'USGS 3DEP', reason: 'service_unavailable', message: 'Timed out' }} loading={false} onRetry={() => {}} />);
    expect(html).toContain('Retry coverage check');
    expect(html).not.toContain('No high-resolution');
  });
  it('limits the inline-direction hint to the along axis', () => {
    const html = renderToStaticMarkup(<InlineAdvisory grade={available} loading={false} />);
    expect(html).toContain('uphill toward the container');
    expect(html).not.toContain('Left side higher');
  });
  it('includes signed estimates, samples and provenance in the summary without the token', () => {
    const text = terrainSummaryLines(available).join('\n');
    expect(text).toContain('-4.00% signed');
    expect(text).toContain('source name: USGS 1 Meter DEM Example');
    expect(text).toContain('2026-09-17T00:00:00Z');
    expect(text).toContain('Sample containerEnd: 47.6000000, -122.3000000');
    expect(text).toContain('not an equipment limit');
    expect(text).not.toContain('server-signed-evidence');
    expect(terrainSummaryLines(null).join('')).toContain('not captured');
  });
});

describe('latest terrain request gate', () => {
  it('aborts the old transport and discards a late response after a placement edit', async () => {
    const gate = createLatestRequest();
    const old = gate.start();
    let resolveOld!: (value: string) => void;
    const response = new Promise<string>(resolve => { resolveOld = resolve; });
    const applied: string[] = [];
    const pending = response.then(value => { if (old.isCurrent()) applied.push(value); });
    gate.invalidate();
    const current = gate.start();
    resolveOld('old placement');
    await pending;
    expect(old.signal.aborted).toBe(true);
    expect(old.isCurrent()).toBe(false);
    expect(current.isCurrent()).toBe(true);
    expect(applied).toEqual([]);
  });
  it('freezes the captured request by invalidating pending work before contact submission', () => {
    const gate = createLatestRequest();
    const pending = gate.start();
    const captured = available;
    gate.invalidate();
    expect(pending.isCurrent()).toBe(false);
    expect(captured.evidenceToken).toBe('server-signed-evidence');
    const refreshed = gate.start();
    expect(refreshed.isCurrent()).toBe(true);
    expect(pending.isCurrent()).toBe(false);
  });
});
