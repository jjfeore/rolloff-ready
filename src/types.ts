export type Size = 10 | 15 | 20 | 30 | 40;
export type Point = { lat: number; lng: number };
export type Address = { id: string; label: string; position: Point; countryCode: 'USA'; verification: string };
export type Placement = Point & { bearingDegrees: number; containerSize: Size };
export type Answers = {
  space: 'yes' | 'no' | 'unsure';
  obstructions: 'no' | 'yes' | 'unsure';
  slope: 'level' | 'sideways' | 'inline' | 'unsure';
  inlineDirection?: 'uphill' | 'downhill' | 'unsure';
  differentPlane: 'no' | 'yes' | 'unsure';
  streetOverlap: 'no' | 'yes' | 'unsure';
};
export type AssessmentInput = { address: Address; placement: Placement; answers: Answers };
export type Assessment = { outcome: 'likely_suitable' | 'review_needed'; headline: string; reasons: string[]; summary: string };
export type Config = { mapsConfigured: boolean; emailConfigured: boolean; gradeEnabled: boolean; formToken: string; attribution: string };
export type Grade = { status: 'available' | 'unavailable'; message: string; source: string; streetGradePercent?: number; roadBearingDegrees?: number; alongTruckPercent?: number; acrossTruckPercent?: number; roadDistanceMeters?: number };
export type Customer = { name: string; email: string; phone: string; notes: string; consent: boolean };
