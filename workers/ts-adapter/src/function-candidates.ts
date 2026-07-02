import ts from "typescript";

import { createEmptyBoundaryImpact, createEmptyContextSignals } from "./protocol.ts";
import type { CandidatePayload } from "./protocol.ts";
import { lineSpan, relativePosix } from "./project.ts";
import {
  collectTopLevelFunctionLikes,
  countSymbolReferences,
  duplicateFunctionKey,
  isExportedNode,
  passthroughTarget,
} from "./syntax.ts";

const LONG_FUNCTION_THRESHOLD = 40;
const DUPLICATE_FUNCTION_MIN_LINES = 3;
const INLINE_FUNCTION_MAX_LINES = 8;

export function buildLongFunctionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const totalLines = sourceFile.getLineAndCharacterOfPosition(sourceFile.end).line + 1;
  const candidates: CandidatePayload[] = [];

  for (const entry of collectTopLevelFunctionLikes(sourceFile)) {
    const span = lineSpan(sourceFile, entry.declaration);
    if (span.length < LONG_FUNCTION_THRESHOLD) {
      continue;
    }
    candidates.push({
      id: `ts-extract-function-${relPath}-${span.startLine}-${entry.nameNode.text}`,
      kind: "extract_function",
      title: `Extract logic from long function ${entry.nameNode.text}`,
      description: `Function \`${entry.nameNode.text}\` in ${relPath} spans ${span.length} lines and is a candidate for extraction`,
      language: "typescript",
      scope: "local",
      source: ["static", "metric"],
      files: [relPath],
      symbols: [entry.nameNode.text],
      anchorRegions: [{ file: relPath, startLine: span.startLine, endLine: span.endLine }],
      estimatedBenefit: {
        complexityReduction: Math.min(1, span.length / Math.max(totalLines, 1)),
        maintainabilityGain: 0.35,
      },
      estimatedRisk: { semanticRisk: 0.35, testRisk: 0.25, conflictRisk: 0.15 },
      estimatedDiff: {
        filesTouched: 1,
        linesAdded: Math.max(3, Math.floor(span.length / 4)),
        linesModified: span.length,
      },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.68,
      applyModeHint: "guarded",
      requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-long-function"],
        evidence: [`line_span:${span.length}`, `symbol:${entry.nameNode.text}`],
      },
    });
  }

  return candidates;
}

export function buildDuplicateFunctionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const groups = new Map<string, Array<{ name: string; startLine: number; endLine: number; length: number }>>();

  for (const entry of collectTopLevelFunctionLikes(sourceFile)) {
    const duplicateKey = duplicateFunctionKey(sourceFile, entry);
    if (!duplicateKey) {
      continue;
    }
    const span = lineSpan(sourceFile, entry.declaration);
    const groupEntry = {
      name: entry.nameNode.text,
      startLine: span.startLine,
      endLine: span.endLine,
      length: span.length,
    };
    groups.set(duplicateKey, [...(groups.get(duplicateKey) ?? []), groupEntry]);
  }

  const candidates: CandidatePayload[] = [];
  for (const functions of [...groups.values()].sort((left, right) => left[0]!.startLine - right[0]!.startLine)) {
    if (functions.length < 2) {
      continue;
    }
    if (Math.max(...functions.map((entry) => entry.length)) < DUPLICATE_FUNCTION_MIN_LINES) {
      continue;
    }
    const symbols = functions.map((entry) => entry.name);
    const totalLines = functions.reduce((sum, entry) => sum + entry.length, 0);
    candidates.push({
      id: `ts-duplicate-logic-${relPath}-${functions[0]!.startLine}-${functions.length}`,
      kind: "duplicate_logic",
      title: `Consolidate duplicate TypeScript functions in ${relPath}`,
      description: `Functions ${symbols.map((symbol) => `\`${symbol}\``).join(", ")} in ${relPath} share the same structure and are candidates for consolidation`,
      language: "typescript",
      scope: "module",
      source: ["clone", "metric"],
      files: [relPath],
      symbols,
      anchorRegions: functions.map((entry) => ({ file: relPath, startLine: entry.startLine, endLine: entry.endLine })),
      estimatedBenefit: { duplicationReduction: Math.min(1, functions.length / 3), maintainabilityGain: 0.42 },
      estimatedRisk: { semanticRisk: 0.28, apiRisk: 0.12, testRisk: 0.22, conflictRisk: 0.18 },
      estimatedDiff: {
        filesTouched: 1,
        linesAdded: Math.max(3, Math.floor(totalLines / 6)),
        linesModified: totalLines,
      },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.74,
      applyModeHint: "guarded",
      requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-duplicate-function"],
        evidence: symbols.map((symbol) => `symbol:${symbol}`).concat(`duplicateGroupSize:${functions.length}`),
      },
    });
  }

  return candidates;
}

export function buildRemoveAbstractionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const candidates: CandidatePayload[] = [];

  for (const entry of collectTopLevelFunctionLikes(sourceFile)) {
    if (!entry.nameNode.text.startsWith("_")) {
      continue;
    }
    const target = passthroughTarget(entry);
    if (!target) {
      continue;
    }
    const span = lineSpan(sourceFile, entry.declaration);
    candidates.push({
      id: `ts-remove-abstraction-${relPath}-${span.startLine}-${entry.nameNode.text}`,
      kind: "remove_abstraction",
      title: `Inline thin wrapper ${entry.nameNode.text}`,
      description: `Private TypeScript wrapper \`${entry.nameNode.text}\` in ${relPath} only forwards to \`${target}\` and can likely be removed`,
      language: "typescript",
      scope: "module",
      source: ["static", "metric"],
      files: [relPath],
      symbols: [entry.nameNode.text],
      anchorRegions: [{ file: relPath, startLine: span.startLine, endLine: span.endLine }],
      estimatedBenefit: {
        complexityReduction: Math.min(1, span.length / Math.max(LONG_FUNCTION_THRESHOLD, 1)),
        maintainabilityGain: 0.26,
      },
      estimatedRisk: { semanticRisk: 0.2, apiRisk: 0.08, testRisk: 0.18, conflictRisk: 0.12 },
      estimatedDiff: {
        filesTouched: 1,
        linesAdded: Math.max(1, Math.floor(span.length / 3)),
        linesModified: span.length,
      },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.74,
      applyModeHint: "guarded",
      requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-passthrough-wrapper"],
        evidence: [`symbol:${entry.nameNode.text}`, `target:${target}`, `line_span:${span.length}`],
      },
    });
  }

  return candidates;
}

export function buildInlineFunctionCandidates(
  checker: ts.TypeChecker,
  sourceFile: ts.SourceFile,
  root: string,
): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const usageCounts = countSymbolReferences(checker, sourceFile);
  const candidates: CandidatePayload[] = [];

  for (const entry of collectTopLevelFunctionLikes(sourceFile)) {
    if (!entry.nameNode.text.startsWith("_") || isExportedNode(entry.declaration)) {
      continue;
    }
    if (passthroughTarget(entry)) {
      continue;
    }
    const symbol = checker.getSymbolAtLocation(entry.nameNode);
    if (!symbol || (usageCounts.get(symbol) ?? 0) !== 1) {
      continue;
    }
    const span = lineSpan(sourceFile, entry.declaration);
    if (span.length > INLINE_FUNCTION_MAX_LINES) {
      continue;
    }
    candidates.push({
      id: `ts-inline-function-${relPath}-${span.startLine}-${entry.nameNode.text}`,
      kind: "inline_function",
      title: `Inline single-use helper ${entry.nameNode.text}`,
      description: `Private TypeScript helper \`${entry.nameNode.text}\` in ${relPath} is referenced only once and is a candidate for inlining into its caller`,
      language: "typescript",
      scope: "module",
      source: ["static", "metric"],
      files: [relPath],
      symbols: [entry.nameNode.text],
      anchorRegions: [{ file: relPath, startLine: span.startLine, endLine: span.endLine }],
      estimatedBenefit: {
        complexityReduction: Math.min(1, span.length / Math.max(INLINE_FUNCTION_MAX_LINES, 1)),
        maintainabilityGain: 0.24,
      },
      estimatedRisk: { semanticRisk: 0.24, apiRisk: 0.06, testRisk: 0.18, conflictRisk: 0.1 },
      estimatedDiff: {
        filesTouched: 1,
        linesAdded: Math.max(1, Math.floor(span.length / 2)),
        linesModified: span.length,
      },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.72,
      applyModeHint: "guarded",
      requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-single-use-helper"],
        evidence: [`symbol:${entry.nameNode.text}`, "referenceCount:1", `line_span:${span.length}`],
      },
    });
  }

  return candidates;
}
