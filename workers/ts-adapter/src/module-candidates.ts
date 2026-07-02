import ts from "typescript";

import { createEmptyBoundaryImpact, createEmptyContextSignals } from "./protocol.ts";
import type { CandidatePayload } from "./protocol.ts";
import { relativePosix } from "./project.ts";

const LARGE_MODULE_THRESHOLD = 300;
const TOP_LEVEL_STATEMENT_THRESHOLD = 18;

export function buildLargeModuleCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const totalLines = sourceFile.getLineAndCharacterOfPosition(sourceFile.end).line + 1;
  const topLevelStatements = sourceFile.statements.length;
  if (totalLines < LARGE_MODULE_THRESHOLD && topLevelStatements < TOP_LEVEL_STATEMENT_THRESHOLD) {
    return [];
  }

  return [{
    id: `ts-split-large-module-${relPath}`,
    kind: "split_large_module",
    title: `Split large module ${relPath}`,
    description: `Module \`${relPath}\` spans ${totalLines} lines across ${topLevelStatements} top-level statements and should be reviewed for decomposition`,
    language: "typescript",
    scope: "module",
    source: ["metric"],
    files: [relPath],
    symbols: [],
    anchorRegions: [],
    estimatedBenefit: {
      complexityReduction: Math.min(1, totalLines / LARGE_MODULE_THRESHOLD),
      maintainabilityGain: 0.4,
    },
    estimatedRisk: { semanticRisk: 0.45, testRisk: 0.3, conflictRisk: 0.2 },
    estimatedDiff: {
      filesTouched: 1,
      linesAdded: Math.max(8, Math.floor(totalLines / 5)),
      linesModified: totalLines,
    },
    contextSignals: createEmptyContextSignals(),
    boundaryImpact: createEmptyBoundaryImpact(),
    confidence: 0.7,
    applyModeHint: "report_only",
    requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
    dependencies: [],
    conflicts: [],
    provenance: {
      detectors: ["ts-worker-large-module"],
      evidence: [`line_span:${totalLines}`, `top_level_statements:${topLevelStatements}`],
    },
  }];
}
