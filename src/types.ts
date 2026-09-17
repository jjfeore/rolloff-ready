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
export type AssessmentInput = { address: Address; placement: Placement; answers: Answers; terrainEvidence?: string };
export type Assessment = { outcome: 'likely_suitable' | 'review_needed'; headline: string; reasons: string[]; summary: string };
export type Config = { mapsConfigured: boolean; emailConfigured: boolean; gradeEnabled: boolean; formToken: string; attribution: string };
export type TerrainUnavailableReason = 'no_coverage' | 'incomplete_coverage' | 'service_unavailable' | 'unsupported_source';
export type TerrainUnavailable = { status: 'unavailable'; source: 'USGS 3DEP'; message: string; reason?: TerrainUnavailableReason };
export type TerrainProvenance = { resolutionMeters: number; sourceName: string; sourceDate: string | null };
export type TerrainCoverage = TerrainUnavailable | ({ status: 'available'; source: 'USGS 3DEP'; message: string } & TerrainProvenance);
export type TerrainAxis = { percent: number; baselineMeters: number; riseMeters: number; classification: 'low' | 'review'; direction: 'uphill' | 'downhill' | 'level' | 'right_up' | 'left_up' };
export type TerrainSample = Point & { elevationMeters: number };
export type AvailableGrade = TerrainProvenance & {
  status: 'available'; source: 'USGS 3DEP'; message: string;
  thresholdPercent: 3; lateralOffsetMeters: 1.5; estimatedAt: string;
  along: TerrainAxis; across: TerrainAxis;
  samples: { containerEnd: TerrainSample; truckFront: TerrainSample; left: TerrainSample; right: TerrainSample };
};
export type Grade = (TerrainUnavailable | AvailableGrade) & { evidenceToken?: string };
export type Customer = { name: string; email: string; phone: string; notes: string; consent: boolean };
