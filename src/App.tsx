import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import catalog from '../api/rr/catalog.json';
import { errorMessage, getConfig, post } from './api';
import { destination, distance, normalizeBearing } from './geometry';
import Icon from './Icons';
import MapErrorBoundary from './MapErrorBoundary';
import type { Address, Answers, Assessment, AssessmentInput, Config, Customer, Grade, Placement, Size } from './types';

const PlacementMap = lazy(() => import('./PlacementMap'));
const steps = ['Your site', 'Place your box', 'Check the space', 'Your next step'];
const initialCustomer: Customer = { name: '', email: '', phone: '', notes: '', consent: false };
const answerLabels: Record<string, string> = { yes: 'Yes', no: 'No', unsure: 'Not sure', level: 'Level', sideways: 'Slopes sideways', inline: 'Slopes along the truck', uphill: 'Uphill from truck', downhill: 'Downhill from truck' };

function Alert({ children }: { children: ReactNode }) {
  return <div className="alert" role="alert"><Icon name="info" /><div>{children}</div></div>;
}

function SizePicker({ value, onChange }: { value: Size; onChange: (size: Size) => void }) {
  const selected = catalog.containers.find(item => item.size === value)!;
  return <fieldset className="size-picker"><legend>Container size <span>cubic yards</span></legend>
    <div className="size-options">{catalog.containers.map(item => <label key={item.size} className={value === item.size ? 'selected' : ''}>
      <input type="radio" name="containerSize" value={item.size} checked={value === item.size} onChange={() => onChange(item.size as Size)} />
      <span>{item.size}</span><small>yd³</small>
    </label>)}</div>
    <p className="size-description"><span>{selected.description}</span><span>{selected.lengthFeet}′ L × {selected.widthFeet}′ W × {selected.heightFeet}′ H</span></p>
  </fieldset>;
}

function Question({ number, title, hint, value, options, onChange, error }: { number: string; title: string; hint?: string; value?: string; options: [string, string][]; onChange: (value: string) => void; error?: boolean }) {
  return <fieldset className={`question ${error ? 'question-error' : ''}`} tabIndex={-1}>
    <legend><span className="question-number">{number}</span>{title}</legend>
    {hint ? <p className="question-hint">{hint}</p> : null}
    <div className={`answer-options ${options.length === 4 ? 'four-options' : ''}`}>{options.map(([id, label]) => <label key={id} className={value === id ? 'selected' : ''}>
      <input type="radio" name={`question-${number}`} value={id} checked={value === id} onChange={() => onChange(id)} /><span>{label}</span>
    </label>)}</div>
    {error ? <p className="field-error">Choose an answer. “Not sure” is welcome.</p> : null}
  </fieldset>;
}

function ModelNotes() {
  return <details className="model-notes"><summary><Icon name="info" size={15} /> About these dimensions</summary><div>
    <p>These are representative exterior dimensions. Actual containers and trucks vary by provider.</p>
    <p>The amber area is a straight clearance estimate: a {catalog.truck.representativeLengthFeet} ft truck plus 20%, or {catalog.truck.lengthFeet} ft. For this screen, it uses the container’s width. Turning room, stabilizers, overhead clearance and ground strength still need an operator’s review.</p>
    {catalog.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.label} ↗</a>)}
  </div></details>;
}

function PlanPreview({ size }: { size: Size }) {
  const item = catalog.containers.find(container => container.size === size)!;
  return <div className="plan-preview">
    <div className="preview-eyebrow"><span className="tiny-cross">+</span> PLAN AHEAD. PLACE WITH CONFIDENCE.</div>
    <div className="preview-intro"><h2>A little planning.<br /><span>A smoother drop-off.</span></h2><p>See how the container and delivery truck fit together before requesting your quote.</p></div>
    <div className="footprint-preview" aria-label={`Illustrative top-down view: ${size} cubic yard container and ${catalog.truck.lengthFeet} foot straight truck clearance. Not a map.`}>
      <span className="diagram-north">↑<small>TOP VIEW</small></span>
      <div className="dimension-width"><span>{item.widthFeet} ft wide</span></div>
      <div className="diagram-pair">
        <div className="diagram-box" style={{ height: `${item.lengthFeet * 5}px` }}><span>{size}<small>yd³ container</small></span><i /><i /><i /></div>
        <div className="diagram-truck"><div className="diagram-arrow">↑</div><span>Truck clearance<small>{catalog.truck.lengthFeet} ft straight approach</small></span></div>
      </div>
      <div className="dimension-length"><span>{(item.lengthFeet + catalog.truck.lengthFeet).toFixed(1)} ft total</span></div>
      <div className="diagram-annotation">Room for the box.<br /><strong>And the truck that brings it.</strong><span>↖</span></div>
    </div>
    <div className="preview-footer"><span className="preview-note">Illustrative footprint · satellite view appears after address selection</span><span className="preview-number">01 / 04</span></div>
  </div>;
}

function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [configError, setConfigError] = useState('');
  const [stage, setStage] = useState(0);
  const [size, setSize] = useState<Size>(20);
  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<Address[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [address, setAddress] = useState<Address | null>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  const [recenter, setRecenter] = useState(0);
  const [answers, setAnswers] = useState<Partial<Answers>>({});
  const [grade, setGrade] = useState<Grade | null>(null);
  const [gradeLoading, setGradeLoading] = useState(false);
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [assessing, setAssessing] = useState(false);
  const [validation, setValidation] = useState(false);
  const [assessmentError, setAssessmentError] = useState('');
  const [customer, setCustomer] = useState<Customer>(initialCustomer);
  const [website, setWebsite] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState('');
  const [accepted, setAccepted] = useState<{ requestId: string; message: string } | null>(null);
  const requestId = useRef(crypto.randomUUID());
  const gradeSequence = useRef(0);
  const searchSequence = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  const dimensions = catalog.containers.find(item => item.size === size)!;

  useEffect(() => { getConfig().then(setConfig).catch(error => setConfigError(errorMessage(error))); }, []);
  useEffect(() => {
    if (window.matchMedia('(max-width: 700px)').matches) {
      if (stage < 2) window.scrollTo({ top: 0, behavior: 'instant' });
      else sidebar.current?.scrollIntoView({ block: 'start', behavior: 'instant' });
    } else {
      sidebar.current?.scrollTo({ top: 0, behavior: 'instant' });
    }
    heading.current?.focus({ preventScroll: true });
  }, [stage]);

  function invalidate() { setAssessment(null); setAccepted(null); setSendError(''); requestId.current = crypto.randomUUID(); }
  function changeSize(next: Size) {
    setSize(next);
    if (placement) { setPlacement({ ...placement, containerSize: next }); setConfirmed(false); invalidate(); }
  }
  function movePlacement(next: Placement) { setPlacement(next); setConfirmed(false); invalidate(); }
  function nudge(direction: number) {
    if (!placement || !address) return;
    const next = { ...placement, ...destination(placement, direction, 0.3048) };
    if (distance(address.position, next) <= 245) movePlacement(next);
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    const sequence = ++searchSequence.current;
    setSearching(true); setSearchError(''); setCandidates(null);
    try {
      const result = await post<{ items: Address[] }>('geocode', { query: query.trim() });
      if (sequence === searchSequence.current) setCandidates(result.items);
    } catch (error) { if (sequence === searchSequence.current) setSearchError(errorMessage(error)); }
    finally { if (sequence === searchSequence.current) setSearching(false); }
  }
  function selectAddress(item: Address) {
    gradeSequence.current += 1;
    setAddress(item); setQuery(item.label); setCandidates(null);
    setPlacement({ ...item.position, bearingDegrees: 0, containerSize: size });
    setConfirmed(false); setAnswers({}); setGrade(null); setGradeLoading(false); setValidation(false); setAssessmentError('');
    invalidate(); setStage(1);
  }
  async function startCheck() {
    if (!placement || !confirmed || !mapReady) return;
    setStage(2); setGradeLoading(true); setGrade(null);
    const sequence = ++gradeSequence.current;
    try {
      const result = await post<Grade>('grade', { placement });
      if (sequence === gradeSequence.current) setGrade(result);
    } catch {
      if (sequence === gradeSequence.current) setGrade({ status: 'unavailable', source: 'Road grade estimate', message: 'Automatic road information is unavailable here. Your observations below are the basis of this site check.' });
    } finally { if (sequence === gradeSequence.current) setGradeLoading(false); }
  }
  function updateAnswer(key: keyof Answers, value: string) {
    setAnswers(previous => {
      const next = { ...previous, [key]: value };
      if (key === 'slope' && value !== 'inline') delete next.inlineDirection;
      return next;
    });
    setAssessmentError(''); invalidate();
  }
  function assessmentInput(): AssessmentInput {
    return { address: address!, placement: placement!, answers: answers as Answers };
  }
  const requiredKeys: (keyof Answers)[] = ['space', 'obstructions', 'slope', ...(answers.slope === 'inline' ? ['inlineDirection' as const] : []), 'differentPlane', 'streetOverlap'];
  const unanswered = requiredKeys.filter(key => !answers[key]);
  async function assess(event: FormEvent) {
    event.preventDefault(); setValidation(true); setAssessmentError('');
    if (unanswered.length) {
      setTimeout(() => document.querySelector<HTMLElement>('.question-error')?.focus(), 0);
      return;
    }
    setAssessing(true);
    const version = requestId.current;
    try { const result = await post<Assessment>('assess', assessmentInput()); if (version === requestId.current) { setAssessment(result); setStage(3); } }
    catch (error) { setAssessmentError(errorMessage(error)); }
    finally { setAssessing(false); }
  }
  function updateCustomer(key: keyof Customer, value: string | boolean) {
    setCustomer(previous => ({ ...previous, [key]: value }));
    requestId.current = crypto.randomUUID(); setSendError('');
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!customer.consent || !assessment || !config?.emailConfigured || sending) return;
    setSending(true); setSendError('');
    try {
      const result = await post<{ status: 'accepted'; requestId: string; message: string }>('contact', { ...assessmentInput(), customer, website, requestId: requestId.current });
      if (result.status !== 'accepted') throw new Error('Unexpected response');
      setAccepted(result);
    } catch (error) { setSendError(errorMessage(error)); }
    finally { setSending(false); }
  }
  function downloadSummary() {
    if (!address || !placement || !assessment) return;
    const summary = [
      'ROLLOFF READY | SITE SUMMARY',
      `Created: ${new Date().toISOString()}`,
      `Site/request reference: ${accepted?.requestId ?? requestId.current}`,
      accepted ? `Email provider accepted request: ${accepted.requestId}` : 'This is a downloaded summary. Downloading does not send a quote request.',
      '', `Result: ${assessment.headline}`, assessment.summary,
      ...assessment.reasons.map(reason => `- ${reason}`), '',
      `Address: ${address.label}`, `Container: ${size} cubic yards`,
      `Representative exterior: ${dimensions.lengthFeet} ft L x ${dimensions.widthFeet} ft W x ${dimensions.heightFeet} ft H`,
      `Truck clearance: ${catalog.truck.lengthFeet} ft L x ${dimensions.widthFeet} ft W (36 ft representative truck + 20%)`,
      `Placement center: ${placement.lat.toFixed(7)}, ${placement.lng.toFixed(7)}`, `Bearing: ${placement.bearingDegrees.toFixed(1)} degrees clockwise from north`, '',
      `Enough visible space: ${answerLabels[answers.space!]}`, `Obstructions present: ${answerLabels[answers.obstructions!]}`,
      `Drop-site slope: ${answerLabels[answers.slope!]}`, ...(answers.inlineDirection ? [`Inline slope direction: ${answerLabels[answers.inlineDirection]}`] : []),
      `Different slopes or planes: ${answerLabels[answers.differentPlane!]}`, `Street overlap: ${answerLabels[answers.streetOverlap!]}`, '',
      `Automatic evidence: ${grade?.message ?? 'Unavailable'}`, `Evidence source: ${grade?.source ?? 'None'}`,
      ...(grade?.status === 'available' ? [`Street grade: ${grade.streetGradePercent ?? 'Unknown'}%; along truck: ${grade.alongTruckPercent ?? 'Unknown'}%; across truck: ${grade.acrossTruckPercent ?? 'Unknown'}%`] : []), '',
      `Name: ${customer.name || 'Not provided'}`, `Email: ${customer.email || 'Not provided'}`, `Phone: ${customer.phone || 'Not provided'}`, `Notes: ${customer.notes || 'None'}`, '',
      'This is an early site screen, not delivery approval. Container dimensions, access, overhead clearance, ground conditions, permits and unloading safety must be confirmed by the operator.',
      '', 'Dimension sources:', ...catalog.sources.map(source => `${source.label}: ${source.url}`),
    ].join('\n');
    const url = URL.createObjectURL(new Blob([summary], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = 'rolloff-ready-site-summary.txt'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to site check</a>
    <header className="app-header">
      <a className="brand" href="#" onClick={event => { event.preventDefault(); if (!sending && !assessing) setStage(0); }} aria-label="Rolloff Ready, your site"><span className="brand-mark"><Icon name="box" size={27} /></span><span>Rolloff<span className="brand-light">Ready</span><small>A CLEARER PICTURE BEFORE DELIVERY</small></span></a>
      <div className="header-note"><span className="status-dot" /> A little planning goes a long way</div>
      <span className="header-tag">Residential site check</span>
    </header>
    <nav className="progress-nav" aria-label="Site check progress"><ol>{steps.map((label, index) => {
      const canVisit = index === 0 || index === 1 && !!placement || index === 2 && confirmed || index === 3 && !!assessment;
      return <li key={label} className={`${stage === index ? 'current' : ''} ${stage > index ? 'completed' : ''}`}><button type="button" disabled={!canVisit || sending || assessing} onClick={() => setStage(index)} aria-current={stage === index ? 'step' : undefined}><span className="step-number">{stage > index ? <Icon name="check" size={15} /> : `0${index + 1}`}</span><span>{label}</span></button>{index < 3 ? <span className="step-line" /> : null}</li>;
    })}</ol><span className="progress-caption">A few minutes now. Fewer surprises later.</span></nav>

    <main id="main-content" className={`workspace stage-${stage}`}>
      <aside className="sidebar" ref={sidebar} aria-label="Site check steps">
        {stage === 0 ? <>
          <div className="section-eyebrow">01 / YOUR SITE</div>
          <h1 tabIndex={-1} ref={heading}>Good delivery starts<br />with a little space.</h1>
          <p className="intro-text">Find your property, position a container, and check the room around it. We’ll help you take the next step.</p>
          <div className="start-form">
            <SizePicker value={size} onChange={changeSize} />
            <form onSubmit={search} className="address-form">
              <label htmlFor="address">Where will the container go?</label>
              <p className="input-hint" id="address-hint">Enter a US street address, including city and state.</p>
              <div className="address-input"><Icon name="pin" size={20} /><input id="address" name="address" type="search" autoComplete="street-address" placeholder="Street address, city, state" value={query} onChange={event => { setQuery(event.target.value); setCandidates(null); setSearchError(''); searchSequence.current += 1; setSearching(false); }} required minLength={8} maxLength={200} aria-describedby="address-hint" /></div>
              <button className="button primary full" type="submit" disabled={searching || !config?.mapsConfigured}>{searching ? <><span className="spinner" /> Finding your address</> : <>Find my property <Icon name="arrow" /></>}</button>
            </form>
            {!config && !configError ? <p className="small muted" role="status">Connecting to the site check…</p> : null}
            {configError ? <Alert>{configError}<button className="text-button" onClick={() => { setConfigError(''); getConfig(true).then(setConfig).catch(error => setConfigError(errorMessage(error))); }}>Try reconnecting</button></Alert> : null}
            {config && !config.mapsConfigured ? <Alert>Address search and satellite maps are temporarily unavailable. Please try again later.</Alert> : null}
            {searchError ? <Alert>{searchError}</Alert> : null}
            {candidates ? <div className="candidate-results" aria-live="polite">{candidates.length ? <><p className="candidate-heading">Choose your delivery address</p><ul>{candidates.map(item => <li key={item.id}><button onClick={() => selectAddress(item)}><Icon name="pin" size={18} /><span>{item.label}</span><Icon name="chevron" size={16} /></button></li>)}</ul></> : <p className="no-results">We couldn’t find that street address. Add a house number, city and state, then try again. US addresses only.</p>}</div> : null}
          </div>
          <ModelNotes />
          <div className="quiet-note"><Icon name="shield" size={18} /><p>Your answers stay in this tab until you send a request. Address searches use HERE.</p></div>
        </> : null}

        {stage === 1 && address && placement ? <>
          <div className="section-eyebrow">02 / PLACE YOUR BOX</div>
          <h1 tabIndex={-1} ref={heading}>Find its spot.</h1>
          <p className="intro-text">Drag the green box where you’d like it delivered. Rotate the handle so the amber truck area lines up with the approach.</p>
          <div className="address-card"><Icon name="pin" /><span>{address.label}</span><button className="text-button" onClick={() => setStage(0)}>Change</button></div>
          <SizePicker value={size} onChange={changeSize} />
          <div className="dimensions-card"><div><i className="legend-box" /><span>Container footprint</span><strong>{dimensions.lengthFeet} × {dimensions.widthFeet} ft</strong></div><div><i className="legend-truck" /><span>Truck clearance</span><strong>{catalog.truck.lengthFeet} × {dimensions.widthFeet} ft</strong></div><p>{(dimensions.lengthFeet + catalog.truck.lengthFeet).toFixed(1)} ft of straight space, end to end</p></div>
          <div className="placement-controls">
            <div className="control-heading"><label htmlFor="bearing">Orientation</label><output htmlFor="bearing">{Math.round(placement.bearingDegrees)}<span>°</span></output></div>
            <input id="bearing" type="range" min="0" max="359" step="1" value={Math.round(placement.bearingDegrees) % 360} onChange={event => movePlacement({ ...placement, bearingDegrees: Number(event.target.value) })} aria-valuetext={`${Math.round(placement.bearingDegrees)} degrees clockwise from north`} />
            <div className="range-labels"><span>North</span><span>South</span><span>North</span></div>
            <div className="control-actions"><button onClick={() => movePlacement({ ...placement, bearingDegrees: normalizeBearing(placement.bearingDegrees - 5) })} aria-label="Rotate counterclockwise five degrees"><Icon name="rotate" size={16} /> −5°</button><button onClick={() => movePlacement({ ...placement, bearingDegrees: normalizeBearing(placement.bearingDegrees + 5) })} aria-label="Rotate clockwise five degrees">+5° <Icon name="rotate" size={16} style={{ transform: 'scaleX(-1)' }} /></button><button onClick={() => movePlacement({ ...placement, bearingDegrees: 0 })}>Reset angle</button></div>
            <div className="nudge-row"><div><strong>Fine-tune position</strong><span>Move 1 foot at a time</span></div><div className="nudge-buttons">{[['↑', 0, 'north'], ['←', 270, 'west'], ['↓', 180, 'south'], ['→', 90, 'east']].map(([label, direction, name]) => <button key={name} onClick={() => nudge(Number(direction))} aria-label={`Move footprint one foot ${name}`}>{label}</button>)}</div></div>
            <button className="text-button recenter" onClick={() => setRecenter(value => value + 1)}><Icon name="target" size={16} /> Recenter on my placement</button>
          </div>
          <ModelNotes />
          <label className="checkbox-label placement-confirm"><input type="checkbox" checked={confirmed} disabled={!mapReady} onChange={event => setConfirmed(event.target.checked)} /><span>I’ve positioned the box and truck area at my intended drop-off spot.</span></label>
          <button className="button primary full" disabled={!confirmed || !mapReady} onClick={startCheck}>Check the space <Icon name="arrow" /></button>
          {!mapReady ? <p className="small muted">Wait for the satellite view to load before confirming.</p> : null}
        </> : null}

        {stage === 2 && address ? <>
          <div className="section-eyebrow">03 / CHECK THE SPACE</div>
          <h1 tabIndex={-1} ref={heading}>A quick look around.</h1>
          <p className="intro-text">Maps only tell part of the story. Use what you know about the actual site. “Not sure” helps us flag what to review.</p>
          <div className="compact-address"><Icon name="pin" size={17} /><span>{address.label}</span><button className="text-button" onClick={() => setStage(1)}>Edit placement</button></div>
          <div className="grade-note"><Icon name="info" size={17} /><div><strong>{gradeLoading ? 'Checking available road information…' : grade?.status === 'available' ? 'Supplemental road information' : 'Your observations matter'}</strong><p>{gradeLoading ? 'You can continue with the questions below.' : grade?.message ?? 'Your observations below are the basis of this site check.'}</p>{grade?.status === 'available' ? <><p>Road grade: {grade.streetGradePercent?.toFixed(1) ?? 'Unknown'}%. Road estimates do not measure your driveway slope or truck support.</p><small>Source: {grade.source}</small></> : null}</div></div>
          <form onSubmit={assess} className="questionnaire" noValidate>
            <Question number="1" title="Does the whole footprint fit?" hint="Include both the green container and the full amber truck area." value={answers.space} options={[["yes", "Yes"], ["no", "No"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('space', value)} error={validation && !answers.space} />
            <Question number="2" title="Are there any obstructions?" hint="Look for overhead wires, branches, parked cars, gates and anything in the approach." value={answers.obstructions} options={[["no", "None"], ["yes", "Yes"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('obstructions', value)} error={validation && !answers.obstructions} />
            <Question number="3" title="How does the drop-off area slope?" hint="Consider the ground under both the box and the truck." value={answers.slope} options={[["level", "Level"], ["sideways", "Sideways"], ["inline", "Along the truck"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('slope', value)} error={validation && !answers.slope} />
            {answers.slope === 'inline' ? <div className="nested-question"><Question number="3b" title="Which way does the ground slope?" hint="From the truck toward the container’s far end." value={answers.inlineDirection} options={[["uphill", "Uphill"], ["downhill", "Downhill"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('inlineDirection', value)} error={validation && !answers.inlineDirection} /></div> : null}
            <Question number="4" title="Will the truck and box be on different slopes or levels?" hint="For example, a level street meeting a sloping driveway, or a curb between them." value={answers.differentPlane} options={[["no", "Same plane"], ["yes", "Different"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('differentPlane', value)} error={validation && !answers.differentPlane} />
            <Question number="5" title="Does either area overlap the street?" hint="Street placement can need extra coordination or permits." value={answers.streetOverlap} options={[["no", "No"], ["yes", "Yes"], ["unsure", "Not sure"]]} onChange={value => updateAnswer('streetOverlap', value)} error={validation && !answers.streetOverlap} />
            {assessmentError ? <Alert>{assessmentError}</Alert> : null}
            {validation && unanswered.length ? <p className="field-error" role="alert">Please answer all {unanswered.length} remaining {unanswered.length === 1 ? 'question' : 'questions'}.</p> : null}
            <button className="button primary full" type="submit" disabled={assessing}>{assessing ? <><span className="spinner" /> Checking your site</> : <>See my next step <Icon name="arrow" /></>}</button>
            <button className="button quiet full" type="button" onClick={() => setStage(1)}>Back to placement</button>
          </form>
        </> : null}

        {stage === 3 && assessment && address ? <>
          <div className="section-eyebrow">04 / YOUR NEXT STEP</div>
          {accepted ? <div className="accepted-panel" role="status"><span className="result-icon suitable"><Icon name="check" size={26} /></span><h1 tabIndex={-1} ref={heading}>Your request is on its way.</h1><p>The email service accepted your request. The operator still needs to confirm the site and delivery details.</p><p className="small">Request reference<br /><code>{accepted.requestId}</code></p><button className="button secondary full" onClick={downloadSummary}><Icon name="download" size={18} /> Save your summary</button><button className="button quiet full" onClick={() => setStage(2)}>Review my site answers</button></div> : <>
            <div className={`result-panel ${assessment.outcome === 'likely_suitable' ? 'suitable' : 'review'}`}>
              <span className={`result-icon ${assessment.outcome === 'likely_suitable' ? 'suitable' : 'review'}`}><Icon name={assessment.outcome === 'likely_suitable' ? 'check' : 'info'} size={24} /></span>
              <span className="result-kicker">{assessment.outcome === 'likely_suitable' ? 'LOOKING GOOD' : 'LET’S TAKE A CLOSER LOOK'}</span>
              <h1 tabIndex={-1} ref={heading}>{assessment.outcome === 'likely_suitable' ? 'Room to take the next step.' : 'A quick review can clear things up.'}</h1>
              <p>{assessment.outcome === 'likely_suitable' ? 'Your answers suggest this spot could work. Send the details for a quote and final delivery confirmation.' : 'A few site details need an operator’s eye. Send your plan so they can help find the right approach.'}</p>
              {assessment.reasons.length ? <ul className="result-reasons">{assessment.reasons.map(reason => <li key={reason}><Icon name={assessment.outcome === 'likely_suitable' ? 'check' : 'info'} size={15} /><span>{reason}</span></li>)}</ul> : null}
            </div>
            <div className="request-summary"><div><Icon name="box" size={19} /><strong>{size} yd³ container</strong><span>{dimensions.lengthFeet} × {dimensions.widthFeet} ft</span></div><p><Icon name="pin" size={16} />{address.label}</p><button className="text-button" onClick={() => setStage(2)}>Review my answers</button></div>
            <form onSubmit={send} className="contact-form">
              <h2>{assessment.outcome === 'likely_suitable' ? 'Request your quote' : 'Request a delivery review'}</h2><p className="input-hint">Your placement and site answers are included automatically.</p>
              {!config?.emailConfigured ? <div className="email-unavailable"><Icon name="info" size={18} /><p>Email requests are unavailable right now. You can still save your site summary below.</p></div> : null}
              <fieldset disabled={sending} className="contact-fields">
                <label htmlFor="name">Your name <span>required</span></label><input id="name" autoComplete="name" value={customer.name} onChange={event => updateCustomer('name', event.target.value)} required maxLength={100} />
                <label htmlFor="email">Email address <span>required</span></label><input id="email" type="email" autoComplete="email" value={customer.email} onChange={event => updateCustomer('email', event.target.value)} required maxLength={254} />
                <label htmlFor="phone">Phone <span>optional</span></label><input id="phone" type="tel" autoComplete="tel" value={customer.phone} onChange={event => updateCustomer('phone', event.target.value)} maxLength={35} />
                <label htmlFor="notes">Anything else we should know? <span>optional</span></label><textarea id="notes" rows={3} placeholder="Gate access, delivery timing, or a detail you’d like us to check…" value={customer.notes} onChange={event => updateCustomer('notes', event.target.value)} maxLength={2000} /><span className="character-count">{customer.notes.length}/2,000</span>
                <div className="honeypot" aria-hidden="true"><label htmlFor="website">Leave this field blank</label><input id="website" name="website" tabIndex={-1} autoComplete="off" value={website} onChange={event => setWebsite(event.target.value)} /></div>
                <label className="checkbox-label consent"><input type="checkbox" checked={customer.consent} onChange={event => updateCustomer('consent', event.target.checked)} required /><span>I agree to share my contact information and site details with the operator for this request.</span></label>
              </fieldset>
              {sendError ? <Alert>{sendError} You can also save your summary below.<p className="request-reference">Request reference<br /><code>{requestId.current}</code></p></Alert> : null}
              <button type="submit" className="button primary full" disabled={sending || !config?.emailConfigured}>{sending ? <><span className="spinner" /> Sending your request</> : <><Icon name="mail" size={18} /> {assessment.outcome === 'likely_suitable' ? 'Send quote request' : 'Send review request'}</>}</button>
              <button type="button" className="button secondary full download-button" onClick={downloadSummary}><Icon name="download" size={18} /> Download site summary</button><p className="download-caption">Downloading saves a file. It does not send a request.</p>
            </form>
          </>}
        </> : null}
        <footer className="sidebar-footer">An early site check, not delivery approval.<br />Your operator confirms the final fit.</footer>
      </aside>

      <section className={`map-workspace ${stage === 0 ? 'preview-mode' : ''}`} aria-label={stage === 0 ? 'How the placement check works' : 'Your site map'}>
        {stage === 0 || !address || !placement ? <PlanPreview size={size} /> : <>
          <MapErrorBoundary key={address.id} onReady={setMapReady}><Suspense fallback={<div className="map-fallback"><span className="spinner" /> Opening your site map…</div>}><PlacementMap address={address} placement={placement} onChange={movePlacement} attribution={config?.attribution ?? '© HERE'} editable={stage === 1} recenter={recenter} onReady={setMapReady} /></Suspense></MapErrorBoundary>
          <div className="map-bottom-card"><span className="map-bottom-icon"><Icon name={stage === 1 ? 'move' : stage === 2 ? 'shield' : 'box'} size={23} /></span><div><strong>{stage === 1 ? 'One connected footprint. Two things to fit.' : stage === 2 ? 'A map can’t see everything.' : 'Your placement, ready to share.'}</strong><p>{stage === 1 ? 'Drag the box to move. Drag the round handle to rotate. Fine-tune with the controls.' : stage === 2 ? 'Look beyond the image: overhead wires, uneven ground and the approach all matter.' : `${size} yd³ container · ${(dimensions.lengthFeet + catalog.truck.lengthFeet).toFixed(1)} ft total straight clearance · final fit confirmed by your operator`}</p></div><span className="map-card-step">0{stage + 1} / 04</span></div>
        </>}
      </section>
    </main>
  </div>;
}

export default App;
