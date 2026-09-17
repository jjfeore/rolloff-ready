import { describe, expect, it } from 'vitest';
import { bearingBetween, destination, distance, FEET_TO_METERS, footprints, normalizeBearing } from './geometry';

describe('geographic footprint geometry', () => {
  for (const lat of [21.3, 37.8, 47.6, 64.8]) {
    for (const heading of [0, 45, 90, 180, 275]) {
      it(`preserves dimensions and connected edge at ${lat}°, bearing ${heading}°`, () => {
        const center = { lat, lng: -122.33 };
        const shape = footprints(center, heading, 22, 8, 40.8);
        expect(distance(shape.container[0], shape.container[1])).toBeCloseTo(8 * FEET_TO_METERS, 4);
        expect(distance(shape.container[1], shape.container[2])).toBeCloseTo(22 * FEET_TO_METERS, 4);
        expect(distance(shape.truck[1], shape.truck[2])).toBeCloseTo(40.8 * FEET_TO_METERS, 4);
        expect(shape.container[2]).toEqual(shape.truck[1]);
        expect(shape.container[3]).toEqual(shape.truck[0]);
        const headingError = ((bearingBetween(center, shape.rotationHandle) - heading + 540) % 360) - 180;
        expect(headingError).toBeCloseTo(0, 5);
      });
    }
  }
  it('resizes the box and keeps its center and shared edge consistent', () => {
    const center = { lat: 37.8, lng: -122.4 };
    const small = footprints(center, 90, 12, 8, 40.8);
    const large = footprints(center, 90, 22, 8, 40.8);
    expect(distance(center, small.connection)).toBeCloseTo(6 * FEET_TO_METERS, 5);
    expect(distance(center, large.connection)).toBeCloseTo(11 * FEET_TO_METERS, 5);
    expect(distance(large.truck[0], large.truck[3])).toBeCloseTo(distance(small.truck[0], small.truck[3]), 4);
  });
  it('wraps longitude and bearing', () => {
    const start = { lat: 52, lng: 179.99999 };
    const end = destination(start, 90, 30);
    expect(end.lng).toBeLessThan(0);
    expect(distance(start, end)).toBeCloseTo(30, 5);
    expect(normalizeBearing(-5)).toBe(355);
    expect(normalizeBearing(725)).toBe(5);
  });
  it('rejects nonfinite coordinates, headings and dimensions', () => {
    expect(() => destination({ lat: NaN, lng: 0 }, 0, 5)).toThrow();
    expect(() => destination({ lat: 90, lng: 0 }, 0, 5)).toThrow();
    expect(() => destination({ lat: 0, lng: 0 }, Infinity, 5)).toThrow();
    expect(() => footprints({ lat: 0, lng: 0 }, 0, -1, 8, 40)).toThrow();
  });
});
