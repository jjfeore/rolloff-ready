import type { Point } from './types';

export const FEET_TO_METERS = 0.3048;
const EARTH_RADIUS = 6371008.8;
const radians = (degrees: number) => degrees * Math.PI / 180;
const degrees = (radians: number) => radians * 180 / Math.PI;

export function normalizeBearing(value: number): number {
  if (!Number.isFinite(value)) throw new Error('Bearing must be finite.');
  return ((value % 360) + 360) % 360;
}

function validPoint(point: Point): void {
  if (!Number.isFinite(point.lat) || !Number.isFinite(point.lng) || Math.abs(point.lat) > 85 || Math.abs(point.lng) > 180) throw new Error('Invalid geographic position.');
}

/** Spherical destination, appropriate for short physical footprints at every US latitude. */
export function destination(origin: Point, bearingDegrees: number, distanceMeters: number): Point {
  validPoint(origin);
  if (!Number.isFinite(distanceMeters)) throw new Error('Distance must be finite.');
  const heading = radians(normalizeBearing(bearingDegrees));
  const angular = distanceMeters / EARTH_RADIUS;
  const lat1 = radians(origin.lat);
  const lng1 = radians(origin.lng);
  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(angular) + Math.cos(lat1) * Math.sin(angular) * Math.cos(heading));
  const lng2 = lng1 + Math.atan2(Math.sin(heading) * Math.sin(angular) * Math.cos(lat1), Math.cos(angular) - Math.sin(lat1) * Math.sin(lat2));
  return { lat: degrees(lat2), lng: ((degrees(lng2) + 540) % 360) - 180 };
}

export function distance(a: Point, b: Point): number {
  validPoint(a); validPoint(b);
  const dLat = radians(b.lat - a.lat), dLng = radians(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS * Math.asin(Math.sqrt(Math.min(1, h)));
}

export function bearingBetween(a: Point, b: Point): number {
  validPoint(a); validPoint(b);
  const delta = radians(b.lng - a.lng);
  const y = Math.sin(delta) * Math.cos(radians(b.lat));
  const x = Math.cos(radians(a.lat)) * Math.sin(radians(b.lat)) - Math.sin(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.cos(delta);
  return normalizeBearing(degrees(Math.atan2(y, x)));
}

export function footprints(center: Point, bearing: number, boxLengthFeet: number, widthFeet: number, truckLengthFeet: number) {
  if ([boxLengthFeet, widthFeet, truckLengthFeet].some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Dimensions must be positive and finite.');
  const length = boxLengthFeet * FEET_TO_METERS;
  const width = widthFeet * FEET_TO_METERS;
  const truckLength = truckLengthFeet * FEET_TO_METERS;
  const corner = (forward: number, right: number) => destination(center, bearing + degrees(Math.atan2(right, forward)), Math.hypot(forward, right));
  const backLeft = corner(-length / 2, -width / 2);
  const backRight = corner(-length / 2, width / 2);
  const container = [corner(length / 2, -width / 2), corner(length / 2, width / 2), backRight, backLeft];
  const truck = [backLeft, backRight, corner(-length / 2 - truckLength, width / 2), corner(-length / 2 - truckLength, -width / 2)];
  return {
    container, truck,
    connection: destination(center, bearing + 180, length / 2),
    truckCenter: destination(center, bearing + 180, length / 2 + truckLength / 2),
    rotationHandle: destination(center, bearing, length / 2 + 4),
  };
}
