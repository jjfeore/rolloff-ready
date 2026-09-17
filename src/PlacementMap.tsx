import { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import type { Address, Placement, Point } from './types';
import { bearingBetween, destination, distance, footprints } from './geometry';
import catalog from '../api/rr/catalog.json';
import Icon from './Icons';

type Props = { address: Address; placement: Placement; onChange: (placement: Placement) => void; attribution: string; editable: boolean; recenter: number; onReady: (ready: boolean) => void };
type Layers = { map: L.Map; box: L.Polygon; truck: L.Polygon; line: L.Polyline; center: L.Marker; rotation: L.Marker; tiles: L.TileLayer; truckLabel: L.Tooltip };
const markerIcon = (rotation: boolean) => L.divIcon({
  className: `placement-handle ${rotation ? 'rotation-handle' : 'center-handle'}`,
  html: rotation
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 7a8 8 0 1 0 1 8M19 2v5h-5"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v18M3 12h18m-12-6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3m12-6 3 3-3 3"/></svg>',
  iconSize: rotation ? [36, 36] : [36, 36], iconAnchor: [18, 18],
});
const latLngs = (points: Point[]): L.LatLngExpression[] => points.map(point => [point.lat, point.lng]);

export default function PlacementMap(props: Props) {
  const host = useRef<HTMLDivElement>(null);
  const layers = useRef<Layers | null>(null);
  const current = useRef(props);
  const [tileError, setTileError] = useState(false);
  const [boundaryError, setBoundaryError] = useState(false);
  const [tilesLoading, setTilesLoading] = useState(true);
  const [mapZoom, setMapZoom] = useState(20);
  current.current = props;

  useEffect(() => { props.onReady(!tilesLoading && !tileError); }, [tilesLoading, tileError, props.onReady]);

  useEffect(() => {
    if (!host.current) return;
    setTileError(false);
    setTilesLoading(true);
    const latest = current.current;
    const map = L.map(host.current, { zoomControl: false, minZoom: 14, maxZoom: 22, zoomSnap: 0.5, scrollWheelZoom: true }).setView(latest.address.position, 20);
    setMapZoom(20);
    map.on('zoomend', () => setMapZoom(map.getZoom()));
    L.control.zoom({ position: 'topright' }).addTo(map);
    L.control.scale({ position: 'bottomleft', imperial: true, metric: false, maxWidth: 120 }).addTo(map);
    const attributionNode = document.createElement('span');
    attributionNode.textContent = latest.attribution;
    const tiles = L.tileLayer('/api/tiles/{z}/{x}/{y}', { minZoom: 14, maxZoom: 22, maxNativeZoom: 20, attribution: `${attributionNode.innerHTML} · <a href="https://legal.here.com/en-gb/terms/general-content-supplier-terms-and-notices" target="_blank" rel="noopener noreferrer">HERE notices</a>` });
    tiles.on('loading', () => setTilesLoading(true));
    tiles.on('load', () => setTilesLoading(false));
    tiles.on('tileerror', () => { setTileError(true); setTilesLoading(false); });
    tiles.addTo(map);
    const truck = L.polygon([], { color: '#ffce72', fillColor: '#edae46', fillOpacity: 0.32, weight: 2, dashArray: '8 5', className: 'footprint truck-footprint' }).addTo(map);
    const box = L.polygon([], { color: '#a6f0d8', fillColor: '#15796c', fillOpacity: 0.56, weight: 2.5, className: 'footprint box-footprint' }).addTo(map);
    const line = L.polyline([], { color: '#ffffff', weight: 2, dashArray: '4 4', interactive: false }).addTo(map);
    const truckLabel = L.tooltip({ permanent: true, direction: 'center', className: 'map-truck-label', interactive: false }).setContent('TRUCK CLEARANCE').setLatLng(latest.placement).addTo(map);
    const center = L.marker(latest.placement, { draggable: true, icon: markerIcon(false), title: 'Move both footprints. Arrow keys move one foot; hold Shift for five feet.', keyboard: true, autoPan: true }).addTo(map);
    const rotation = L.marker(latest.placement, { draggable: true, icon: markerIcon(true), title: 'Rotate both footprints. Use left and right arrow keys to rotate five degrees.', keyboard: true }).addTo(map);
    layers.current = { map, box, truck, line, center, rotation, tiles, truckLabel };

    const move = (next: Placement) => {
      if (!current.current.editable) return;
      if (distance(current.current.address.position, next) > 245) { setBoundaryError(true); return; }
      setBoundaryError(false);
      current.current.onChange(next);
    };
    center.on('drag', () => move({ ...current.current.placement, lat: center.getLatLng().lat, lng: center.getLatLng().lng }));
    center.on('dragend', () => center.setLatLng(current.current.placement));
    rotation.on('drag', () => move({ ...current.current.placement, bearingDegrees: bearingBetween(current.current.placement, rotation.getLatLng()) }));

    let drag: { point: L.LatLng; placement: Placement } | null = null;
    const start = (event: PointerEvent) => {
      if (!current.current.editable || event.button > 0) return;
      event.preventDefault(); event.stopPropagation();
      drag = { point: map.mouseEventToLatLng(event), placement: { ...current.current.placement } };
      map.dragging.disable();
      map.touchZoom.disable();
    };
    const during = (event: PointerEvent) => {
      if (!drag) return;
      event.preventDefault();
      const point = map.mouseEventToLatLng(event);
      const lng = ((drag.placement.lng + point.lng - drag.point.lng + 540) % 360) - 180;
      move({ ...drag.placement, lat: drag.placement.lat + point.lat - drag.point.lat, lng });
    };
    const end = () => { if (drag) { drag = null; map.dragging.enable(); map.touchZoom.enable(); } };
    const boxElement = box.getElement()!;
    const truckElement = truck.getElement()!;
    boxElement.addEventListener('pointerdown', start as EventListener);
    truckElement.addEventListener('pointerdown', start as EventListener);
    document.addEventListener('pointermove', during, { passive: false });
    document.addEventListener('pointerup', end);
    document.addEventListener('pointercancel', end);
    const onKey = (rotationOnly: boolean) => (event: KeyboardEvent) => {
      if (!current.current.editable) return;
      const heading: Record<string, number> = { ArrowUp: 0, ArrowRight: 90, ArrowDown: 180, ArrowLeft: 270 };
      if (!(event.key in heading)) return;
      event.preventDefault(); event.stopPropagation();
      if (rotationOnly) move({ ...current.current.placement, bearingDegrees: (current.current.placement.bearingDegrees + (event.key === 'ArrowLeft' || event.key === 'ArrowDown' ? -5 : 5) + 360) % 360 });
      else move({ ...current.current.placement, ...destination(current.current.placement, heading[event.key], event.shiftKey ? 1.524 : 0.3048) });
    };
    const centerElement = center.getElement()!;
    const rotationElement = rotation.getElement()!;
    centerElement.setAttribute('aria-label', 'Move footprint. Arrow keys move one foot; Shift moves five feet.');
    rotationElement.setAttribute('aria-label', 'Rotate footprint. Arrow keys rotate five degrees.');
    const centerKey = onKey(false), rotationKey = onKey(true);
    centerElement.addEventListener('keydown', centerKey);
    rotationElement.addEventListener('keydown', rotationKey);
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      boxElement.removeEventListener('pointerdown', start as EventListener);
      truckElement.removeEventListener('pointerdown', start as EventListener);
      document.removeEventListener('pointermove', during);
      document.removeEventListener('pointerup', end);
      document.removeEventListener('pointercancel', end);
      map.remove(); layers.current = null;
    };
  }, [props.address.id]);

  useEffect(() => {
    const layer = layers.current;
    if (!layer) return;
    const dimensions = catalog.containers.find(item => item.size === props.placement.containerSize)!;
    const shape = footprints(props.placement, props.placement.bearingDegrees, dimensions.lengthFeet, dimensions.widthFeet, catalog.truck.lengthFeet);
    layer.box.setLatLngs(latLngs(shape.container));
    layer.truck.setLatLngs(latLngs(shape.truck));
    layer.line.setLatLngs([props.placement, shape.rotationHandle]);
    layer.center.setLatLng(props.placement);
    layer.rotation.setLatLng(shape.rotationHandle);
    layer.truckLabel.setLatLng(shape.truckCenter);
    layer.center.getElement()!.style.display = props.editable ? '' : 'none';
    layer.rotation.getElement()!.style.display = props.editable ? '' : 'none';
    layer.line.setStyle({ opacity: props.editable ? 1 : 0 });
    if (props.editable) { layer.center.dragging?.enable(); layer.rotation.dragging?.enable(); }
    else { layer.center.dragging?.disable(); layer.rotation.dragging?.disable(); }
  }, [props.placement, props.editable, props.address.id]);

  useEffect(() => {
    const map = layers.current?.map;
    if (!map) return;
    const placement = current.current.placement;
    const dimensions = catalog.containers.find(item => item.size === placement.containerSize)!;
    const shape = footprints(placement, placement.bearingDegrees, dimensions.lengthFeet, dimensions.widthFeet, catalog.truck.lengthFeet);
    const bounds = L.latLngBounds(latLngs([...shape.container, ...shape.truck, shape.rotationHandle]));
    map.fitBounds(bounds, { paddingTopLeft: [32, 48], paddingBottomRight: [32, 56], maxZoom: 20, animate: false });
  }, [props.recenter, props.address.id]);

  return <div className="live-map">
    <div ref={host} className="leaflet-host" aria-label="Satellite map of your selected address with container and truck clearance footprints" />
    <div className="map-top-label"><span className="live-dot" /> SATELLITE VIEW <span className="map-label-divider" /> <span>North up</span></div>
    {mapZoom > 20 ? <div className="imagery-enlarged">Imagery enlarged · no additional detail</div> : null}
    {tilesLoading && !tileError ? <div className="map-loading" role="status"><span className="spinner" /> Loading satellite imagery</div> : null}
    {tileError ? <div className="map-error" role="alert"><Icon name="info" /><div><strong>Satellite imagery could not load.</strong><p>Check your connection and retry before confirming your placement.</p><button onClick={() => { setTileError(false); setTilesLoading(true); layers.current?.tiles.redraw(); }}>Retry imagery</button></div></div> : null}
    {boundaryError ? <div className="map-boundary" role="status">Keep your placement near the selected address.</div> : null}
    <div className="map-legend"><span><i className="legend-box" /> {props.placement.containerSize} yd³ container</span><span><i className="legend-truck" /> Truck clearance</span></div>
  </div>;
}
