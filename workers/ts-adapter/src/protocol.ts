export const PROTOCOL_VERSION = 1;
export const PROTOCOL_CAPABILITIES = ["scan", "verify", "deterministic-ordering", "typescript-semantic-candidates"];

export function createEmptyContextSignals() {
  return {
    coverageRatio: null,
    hotspotScore: null,
    churnScore: null,
    fanIn: null,
    fanOut: null,
    publicApiExposure: false,
    benchmarkAvailable: false,
  };
}

export function createEmptyBoundaryImpact() {
  return {
    crossLanguage: false,
    boundaryTypes: [] as string[],
    producerSide: [] as string[],
    consumerSide: [] as string[],
    contractArtifacts: [] as string[],
    impactLevel: "none" as const,
  };
}

export function createLayerBoundaryImpact(producerSide: string[], consumerSide: string[]) {
  return {
    crossLanguage: false,
    boundaryTypes: [] as string[],
    producerSide,
    consumerSide,
    contractArtifacts: [] as string[],
    impactLevel: "medium" as const,
  };
}

export type WorkerCommand = "scan" | "verify";

export type WorkerRequest = {
  protocolVersion: number;
  capabilities?: string[];
  command: WorkerCommand;
  root: string;
};

export type WorkerError = {
  code: string;
  message: string;
  details?: Record<string, unknown>;
};

export type WorkerFailure = {
  protocolVersion: number;
  capabilities: string[];
  ok: false;
  command: WorkerCommand;
  error: WorkerError;
};

export type VerificationCheckPayload = {
  name: string;
  kind: "parse" | "typecheck" | "lint" | "build" | "unit_test";
  status: "passed" | "failed" | "skipped";
  evidence: string[];
  details: Record<string, unknown>;
};

type WorkerSuccess = {
  protocolVersion: number;
  capabilities: string[];
  ok: true;
  command: WorkerCommand;
};

export type WorkerScanSuccess = WorkerSuccess & {
  command: "scan";
  candidates: CandidatePayload[];
};

export type WorkerVerifySuccess = WorkerSuccess & {
  command: "verify";
  checks: VerificationCheckPayload[];
};

export type WorkerResponse = WorkerFailure | WorkerScanSuccess | WorkerVerifySuccess;

export type CandidatePayload = {
  id: string;
  kind: "unused_import" | "unused_symbol" | "extract_function" | "inline_function" | "duplicate_logic" | "remove_abstraction" | "split_large_module" | "reduce_cycle" | "layer_violation_fix" | "move_symbol";
  title: string;
  description: string;
  language: "typescript";
  scope: "local" | "module" | "package";
  source: Array<"static" | "clone" | "metric" | "graph">;
  files: string[];
  symbols: string[];
  anchorRegions: Array<{ file: string; startLine: number; endLine: number }>;
  estimatedBenefit: {
    complexityReduction?: number;
    duplicationReduction?: number;
    maintainabilityGain?: number;
  };
  estimatedRisk: {
    semanticRisk: number;
    apiRisk?: number;
    testRisk?: number;
    conflictRisk: number;
  };
  estimatedDiff: {
    filesTouched: number;
    linesAdded?: number;
    linesDeleted?: number;
    linesModified: number;
  };
  contextSignals: {
    coverageRatio: null;
    hotspotScore: null;
    churnScore: null;
    fanIn: null;
    fanOut: null;
    publicApiExposure: boolean;
    benchmarkAvailable: boolean;
  };
  boundaryImpact: {
    crossLanguage: boolean;
    boundaryTypes: string[];
    producerSide: string[];
    consumerSide: string[];
    contractArtifacts: string[];
    impactLevel: "none" | "low" | "medium" | "high";
  };
  confidence: number;
  applyModeHint: "auto" | "guarded" | "report_only";
  requiredChecks: Array<"parse" | "lint" | "typecheck" | "unit_test">;
  dependencies: string[];
  conflicts: string[];
  provenance: {
    detectors: string[];
    evidence: string[];
  };
};
