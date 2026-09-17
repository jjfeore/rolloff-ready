import Icon from './Icons';
import { terrainAxisView, terrainDate, terrainUnavailableText } from './terrain';
import type { Grade, TerrainCoverage, TerrainAxis } from './types';

export function CoverageStatus({ coverage, loading, onRetry }: { coverage: TerrainCoverage | null; loading: boolean; onRetry: () => void }) {
  const unavailable = coverage?.status === 'unavailable' ? terrainUnavailableText(coverage) : null;
  return <div className="terrain-coverage" role="status">
    {loading ? <span className="spinner" /> : <Icon name="info" size={16} />}
    <div><strong>{loading ? 'Checking USGS terrain coverage…' : coverage?.status === 'available' ? 'USGS 1 m terrain data is available' : unavailable?.title ?? 'USGS terrain has not been checked'}</strong>
      <p>{loading ? 'Keep positioning your container while we check.' : coverage?.status === 'available' ? 'Confirm your placement to estimate slope in both directions.' : 'You can continue placing your box and check the site yourself.'}</p>
      {coverage?.status === 'available' ? <small>{coverage.sourceName} · source date: {terrainDate(coverage.sourceDate)}</small> : null}
      {!loading && (!coverage || coverage.status === 'unavailable' && (!coverage.reason || coverage.reason === 'service_unavailable')) ? <button type="button" className="text-button" onClick={onRetry}>Retry coverage check</button> : null}
    </div>
  </div>;
}

function AxisCard({ axis, orientation, threshold }: { axis: TerrainAxis; orientation: 'along' | 'across'; threshold: number }) {
  const view = terrainAxisView(axis, orientation, threshold);
  return <div className={`terrain-axis ${view?.tone ?? 'unknown'}`}>
    <span className="terrain-axis-label">{orientation === 'along' ? 'Along truck → container' : 'Across the footprint'}</span>
    {view ? <>
      <div className="terrain-axis-value"><strong>{view.percent}</strong><Icon name={view.tone === 'low' ? 'check' : 'info'} size={17} /></div>
      <span className="terrain-axis-cue">{view.description}</span>
      <span>{view.direction}</span><small>Sampled over {view.baseline}</small>
    </> : <><strong className="terrain-unknown">Estimate unavailable</strong><span>Confirm this direction on site.</span></>}
  </div>;
}

export function GradeAdvisory({ grade, loading, onRefresh }: { grade: Grade | null; loading: boolean; onRefresh: () => void }) {
  const unavailable = grade?.status === 'unavailable' ? terrainUnavailableText(grade) : null;
  return <div className="terrain-advisory" aria-live="polite">
    <div className="terrain-heading"><Icon name="info" size={15} /><strong>Terrain estimate · confirm with what you see</strong></div>
    {loading ? <div className="terrain-pending" role="status"><span className="spinner" /><p>Checking four USGS terrain samples. Keep answering from your own observations.</p></div>
      : grade?.status === 'available' ? <>
        <div className="terrain-axes"><AxisCard axis={grade.along} orientation="along" threshold={grade.thresholdPercent} /><AxisCard axis={grade.across} orientation="across" threshold={grade.thresholdPercent} /></div>
        <p className="terrain-direction-key">Left and right are viewed from the truck toward the container.</p>
        <p className="terrain-provenance"><strong>{grade.source} · {grade.resolutionMeters} m terrain grid</strong><span>{grade.sourceName}</span><span>Source date: {terrainDate(grade.sourceDate)}</span></p>
        <details className="terrain-method"><summary>How this estimate works</summary><p>Four elevation samples compare the container’s far end with the truck’s front, plus points {grade.lateralOffsetMeters} m beyond each side edge at the midpoint of the combined truck and container footprint. Slope is elevation change divided by the sampled distance.</p><p>At {grade.thresholdPercent}% or more, we flag a noticeable estimated slope. This is a tentative review cue, not an equipment limit or a delivery decision. Grid spacing does not establish vertical accuracy.</p><p>These samples cannot resolve every curb, grade break or separate support plane. Confirm the actual ground conditions in the answers below.</p><p>Estimate made: {new Date(grade.estimatedAt).toLocaleString()}</p></details>
      </> : <div className="terrain-unavailable"><strong>{unavailable?.title ?? 'No estimate for this placement yet'}</strong><p>{unavailable?.detail ?? 'Your observations are the basis of this site check. You can request a terrain estimate without changing your answers.'}</p></div>}
    <p className="terrain-confirm-note">Choose each answer yourself. Terrain estimates do not select answers or approve delivery.</p>
    {!loading ? <button type="button" className="text-button" onClick={onRefresh}>Refresh terrain estimates</button> : null}
  </div>;
}

export function InlineAdvisory({ grade, loading }: { grade: Grade | null; loading: boolean }) {
  const along = !loading && grade?.status === 'available' ? terrainAxisView(grade.along, 'along', grade.thresholdPercent) : null;
  return <p className={`terrain-inline ${along?.tone ?? ''}`}><Icon name="info" size={14} /><span>{loading ? 'The along-direction estimate is loading. Choose what you observe.' : along ? `Along-direction estimate: ${along.direction.toLowerCase()} (${along.percent} over ${along.baseline}). Confirm what you observe.` : 'No along-direction estimate is available. Choose what you observe.'}</span></p>;
}
