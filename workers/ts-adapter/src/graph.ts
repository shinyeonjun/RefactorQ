import { join, resolve } from "node:path";
import ts from "typescript";

import { createEmptyBoundaryImpact, createEmptyContextSignals, createLayerBoundaryImpact } from "./protocol.ts";
import type { CandidatePayload } from "./protocol.ts";
import { relativePosix } from "./project.ts";

const CLIENT_LAYER_TOKENS = new Set(["frontend", "client", "web", "ui"]);
const SERVER_LAYER_TOKENS = new Set(["backend", "server", "api", "controller", "controllers"]);

function pathLayer(relPath: string): "client" | "server" | null {
  const tokens = new Set(relPath.split("/").map((part) => part.toLowerCase()));
  for (const token of CLIENT_LAYER_TOKENS) {
    if (tokens.has(token)) {
      return "client";
    }
  }
  for (const token of SERVER_LAYER_TOKENS) {
    if (tokens.has(token)) {
      return "server";
    }
  }
  return null;
}

function resolveLocalImport(specifier: string, importer: string, knownFiles: Set<string>): string | null {
  if (!specifier.startsWith(".")) {
    return null;
  }
  const base = resolve(importer, "..", specifier);
  const candidates = [
    `${base}.ts`,
    `${base}.tsx`,
    `${base}.js`,
    `${base}.jsx`,
    join(base, "index.ts"),
    join(base, "index.tsx"),
    join(base, "index.js"),
    join(base, "index.jsx"),
  ];
  for (const candidate of candidates) {
    if (knownFiles.has(candidate)) {
      return candidate;
    }
  }
  return null;
}

function stronglyConnectedComponents(root: string, graph: Map<string, Set<string>>): string[][] {
  let index = 0;
  const stack: string[] = [];
  const onStack = new Set<string>();
  const indices = new Map<string, number>();
  const lowlinks = new Map<string, number>();
  const components: string[][] = [];

  const visit = (node: string): void => {
    indices.set(node, index);
    lowlinks.set(node, index);
    index += 1;
    stack.push(node);
    onStack.add(node);

    for (const neighbor of graph.get(node) ?? []) {
      if (!indices.has(neighbor)) {
        visit(neighbor);
        lowlinks.set(node, Math.min(lowlinks.get(node) ?? 0, lowlinks.get(neighbor) ?? 0));
      } else if (onStack.has(neighbor)) {
        lowlinks.set(node, Math.min(lowlinks.get(node) ?? 0, indices.get(neighbor) ?? 0));
      }
    }

    if ((lowlinks.get(node) ?? 0) !== (indices.get(node) ?? 0)) {
      return;
    }

    const component: string[] = [];
    while (stack.length > 0) {
      const member = stack.pop();
      if (!member) {
        break;
      }
      onStack.delete(member);
      component.push(member);
      if (member === node) {
        break;
      }
    }
    components.push(component.sort((left, right) => relativePosix(root, left).localeCompare(relativePosix(root, right))));
  };

  for (const node of [...graph.keys()].sort((left, right) => relativePosix(root, left).localeCompare(relativePosix(root, right)))) {
    if (!indices.has(node)) {
      visit(node);
    }
  }
  return components;
}

function importSymbols(clause: ts.ImportClause | undefined): string[] {
  if (!clause) {
    return [];
  }
  const symbols: string[] = [];
  if (clause.name) {
    symbols.push(clause.name.text);
  }
  const bindings = clause.namedBindings;
  if (bindings && ts.isNamedImports(bindings)) {
    for (const element of bindings.elements) {
      symbols.push(element.name.text);
    }
  }
  return symbols;
}

export function buildLayerViolationCandidates(root: string, files: string[], program: ts.Program): CandidatePayload[] {
  const knownFiles = new Set(files);
  const candidates: CandidatePayload[] = [];

  for (const fileName of files) {
    const sourceFile = program.getSourceFile(fileName);
    if (!sourceFile || sourceFile.isDeclarationFile) {
      continue;
    }
    const relPath = relativePosix(root, fileName);
    const currentLayer = pathLayer(relPath);
    if (!currentLayer) {
      continue;
    }
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) {
        continue;
      }
      const target = resolveLocalImport(statement.moduleSpecifier.text, fileName, knownFiles);
      if (!target) {
        continue;
      }
      const targetRelPath = relativePosix(root, target);
      const targetLayer = pathLayer(targetRelPath);
      if (!targetLayer || targetLayer === currentLayer) {
        continue;
      }
      const startLine = sourceFile.getLineAndCharacterOfPosition(statement.getStart(sourceFile)).line + 1;
      const symbols = importSymbols(statement.importClause);
      candidates.push({
        id: `ts-layer-violation-${relPath}-${startLine}-${targetRelPath.replaceAll("/", "-")}`,
        kind: "layer_violation_fix",
        title: `Review layer-violating import in ${relPath}`,
        description: `Import at line ${startLine} crosses between \`${relPath}\` and \`${targetRelPath}\`, suggesting a layer boundary violation`,
        language: "typescript",
        scope: "package",
        source: ["graph"],
        files: [relPath, targetRelPath],
        symbols,
        anchorRegions: [{ file: relPath, startLine, endLine: startLine }],
        estimatedBenefit: { maintainabilityGain: 0.32 },
        estimatedRisk: { semanticRisk: 0.22, apiRisk: 0.1, testRisk: 0.18, conflictRisk: 0.12 },
        estimatedDiff: { filesTouched: 2, linesAdded: 4, linesModified: 8 },
        contextSignals: createEmptyContextSignals(),
        boundaryImpact: createLayerBoundaryImpact([targetRelPath], [relPath]),
        confidence: 0.69,
        applyModeHint: "report_only",
        requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
        dependencies: [],
        conflicts: [],
        provenance: {
          detectors: ["ts-worker-layer-violation"],
          evidence: [`line:${startLine}`, `target:${targetRelPath}`],
        },
      });
      if (symbols.length > 0) {
        candidates.push({
          id: `ts-move-symbol-${relPath}-${startLine}-${targetRelPath.replaceAll("/", "-")}`,
          kind: "move_symbol",
          title: `Review moving imported boundary symbols from ${targetRelPath}`,
          description: `Imported symbols ${symbols.map((symbol) => `\`${symbol}\``).join(", ")} cross between \`${relPath}\` and \`${targetRelPath}\` and may need relocation behind a clearer module boundary`,
          language: "typescript",
          scope: "package",
          source: ["graph"],
          files: [relPath, targetRelPath],
          symbols,
          anchorRegions: [{ file: relPath, startLine, endLine: startLine }],
          estimatedBenefit: { maintainabilityGain: 0.28 },
          estimatedRisk: { semanticRisk: 0.28, apiRisk: 0.16, testRisk: 0.22, conflictRisk: 0.14 },
          estimatedDiff: { filesTouched: 2, linesAdded: 6, linesModified: 10 },
          contextSignals: createEmptyContextSignals(),
          boundaryImpact: createLayerBoundaryImpact([targetRelPath], [relPath]),
          confidence: 0.64,
          applyModeHint: "report_only",
          requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
          dependencies: [],
          conflicts: [],
          provenance: {
            detectors: ["ts-worker-move-symbol"],
            evidence: [`line:${startLine}`, `target:${targetRelPath}`, ...symbols.map((symbol) => `symbol:${symbol}`)],
          },
        });
      }
    }
  }

  return candidates;
}

export function buildCycleCandidates(root: string, files: string[], program: ts.Program): CandidatePayload[] {
  const knownFiles = new Set(files);
  const graph = new Map<string, Set<string>>();
  for (const fileName of files) {
    graph.set(fileName, new Set<string>());
    const sourceFile = program.getSourceFile(fileName);
    if (!sourceFile || sourceFile.isDeclarationFile) {
      continue;
    }
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement)) {
        continue;
      }
      if (!ts.isStringLiteral(statement.moduleSpecifier)) {
        continue;
      }
      const target = resolveLocalImport(statement.moduleSpecifier.text, fileName, knownFiles);
      if (target) {
        graph.get(fileName)?.add(target);
      }
    }
  }

  return stronglyConnectedComponents(root, graph)
    .filter((component) => component.length > 1)
    .map((component) => {
      const relFiles = component.map((fileName) => relativePosix(root, fileName));
      const cycleId = relFiles.join("-").replaceAll("/", "-").replaceAll(".", "-");
      return {
        id: `ts-reduce-cycle-${cycleId}`,
        kind: "reduce_cycle",
        title: `Reduce import cycle across ${relFiles.length} TypeScript modules`,
        description: `TypeScript import cycle detected across ${relFiles.map((fileName) => `\`${fileName}\``).join(", ")}`,
        language: "typescript",
        scope: "package",
        source: ["graph"],
        files: relFiles,
        symbols: relFiles,
        anchorRegions: [],
        estimatedBenefit: { cycleReduction: 1, maintainabilityGain: 0.38 },
        estimatedRisk: { semanticRisk: 0.42, testRisk: 0.28, conflictRisk: 0.24 },
        estimatedDiff: {
          filesTouched: relFiles.length,
          linesAdded: Math.max(4, relFiles.length * 3),
          linesModified: Math.max(2, relFiles.length * 4),
        },
        contextSignals: createEmptyContextSignals(),
        boundaryImpact: createEmptyBoundaryImpact(),
        confidence: 0.74,
        applyModeHint: "report_only",
        requiredChecks: ["parse", "lint", "typecheck", "unit_test"],
        dependencies: [],
        conflicts: [],
        provenance: {
          detectors: ["ts-worker-import-graph-cycle"],
          evidence: relFiles.map((fileName) => `file:${fileName}`),
        },
      };
    });
}
