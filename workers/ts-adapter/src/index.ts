import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";

const PROTOCOL_VERSION = 1;
const PROTOCOL_CAPABILITIES = ["scan", "verify", "deterministic-ordering", "typescript-semantic-candidates"];
const LONG_FUNCTION_THRESHOLD = 40;
const LARGE_MODULE_THRESHOLD = 300;
const DUPLICATE_FUNCTION_MIN_LINES = 3;
const TOP_LEVEL_STATEMENT_THRESHOLD = 18;
const IGNORED = new Set([
  ".git",
  ".venv",
  "node_modules",
  "dist",
  "build",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  "coverage",
]);
const SUPPORTED_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx"]);

function createEmptyContextSignals() {
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

function createEmptyBoundaryImpact() {
  return {
    crossLanguage: false,
    boundaryTypes: [] as string[],
    producerSide: [] as string[],
    consumerSide: [] as string[],
    contractArtifacts: [] as string[],
    impactLevel: "none" as const,
  };
}

type WorkerCommand = "scan" | "verify";

type WorkerRequest = {
  protocolVersion: number;
  capabilities?: string[];
  command: WorkerCommand;
  root: string;
};

type WorkerError = {
  code: string;
  message: string;
  details?: Record<string, unknown>;
};

type WorkerFailure = {
  protocolVersion: number;
  capabilities: string[];
  ok: false;
  command: WorkerCommand;
  error: WorkerError;
};

type VerificationCheckPayload = {
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

type WorkerScanSuccess = WorkerSuccess & {
  command: "scan";
  candidates: CandidatePayload[];
};

type WorkerVerifySuccess = WorkerSuccess & {
  command: "verify";
  checks: VerificationCheckPayload[];
};

type WorkerResponse = WorkerFailure | WorkerScanSuccess | WorkerVerifySuccess;

type CandidatePayload = {
  id: string;
  kind: "unused_import" | "unused_symbol" | "extract_function" | "duplicate_logic" | "remove_abstraction" | "split_large_module" | "reduce_cycle";

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

function relativePosix(root: string, full: string): string {
  return relative(root, full).replaceAll("\\", "/");
}

function walk(root: string, current: string, files: string[]): void {
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (IGNORED.has(entry.name)) continue;
    const full = join(current, entry.name);
    if (entry.isDirectory()) {
      walk(root, full, files);
      continue;
    }
    if (!entry.isFile()) {
      continue;
    }
    const extension = entry.name.slice(entry.name.lastIndexOf("."));
    if (SUPPORTED_EXTENSIONS.has(extension)) {
      files.push(full);
    }
  }
}

function collectSourceFiles(rootArg: string): string[] {
  const root = resolve(rootArg);
  const stats = statSync(root);
  if (!stats.isDirectory()) {
    throw new Error(`Expected directory: ${root}`);
  }
  const files: string[] = [];
  walk(root, root, files);
  return files.sort((left, right) => relativePosix(root, left).localeCompare(relativePosix(root, right)));
}

function createProgram(rootArg: string): { root: string; files: string[]; program: ts.Program } {
  const root = resolve(rootArg);
  const files = collectSourceFiles(root);
  const program = ts.createProgram(files, {
    allowJs: true,
    checkJs: false,
    noEmit: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    jsx: ts.JsxEmit.Preserve,
  });
  return { root, files, program };
}

function sortCandidates(candidates: CandidatePayload[]): CandidatePayload[] {
  return candidates.sort((left, right) => {
    const leftFile = left.files[0] ?? "";
    const rightFile = right.files[0] ?? "";
    if (leftFile !== rightFile) {
      return leftFile.localeCompare(rightFile);
    }
    const leftLine = left.anchorRegions[0]?.startLine ?? 0;
    const rightLine = right.anchorRegions[0]?.startLine ?? 0;
    if (leftLine !== rightLine) {
      return leftLine - rightLine;
    }
    return left.id.localeCompare(right.id);
  });
}

function lineSpan(sourceFile: ts.SourceFile, node: ts.Node): { startLine: number; endLine: number; length: number } {
  const startLine = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  const endLine = sourceFile.getLineAndCharacterOfPosition(node.end).line + 1;
  return { startLine, endLine, length: endLine - startLine + 1 };
}

function duplicateFunctionKey(sourceFile: ts.SourceFile, node: ts.FunctionDeclaration): string | null {
  if (!node.body) {
    return null;
  }
  const parameters = node.parameters.map((parameter) => parameter.getText(sourceFile).replace(/\s+/g, " ").trim()).join(",");
  const body = node.body.statements.map((statement) => statement.getText(sourceFile).replace(/\s+/g, " ").trim()).join(";");
  if (!body) {
    return null;
  }
  return `${parameters}|${body}`;
}

function passthroughTarget(node: ts.FunctionDeclaration): string | null {
  if (!node.name || !node.body || node.body.statements.length !== 1 || isExportedNode(node)) {
    return null;
  }
  const [statement] = node.body.statements;
  if (!statement || !ts.isReturnStatement(statement) || !statement.expression || !ts.isCallExpression(statement.expression)) {
    return null;
  }
  if (!ts.isIdentifier(statement.expression.expression) || statement.expression.expression.text === node.name.text) {
    return null;
  }
  if (statement.expression.arguments.length !== node.parameters.length) {
    return null;
  }
  const parameterNames = node.parameters.map((parameter) => ts.isIdentifier(parameter.name) ? parameter.name.text : null);
  if (parameterNames.some((name) => name === null)) {
    return null;
  }
  const forwardedNames = statement.expression.arguments.map((argument) => ts.isIdentifier(argument) ? argument.text : null);
  if (forwardedNames.some((name) => name === null)) {
    return null;
  }
  if (parameterNames.join(",") !== forwardedNames.join(",")) {
    return null;
  }
  return statement.expression.expression.text;
}

function isImportedIdentifier(node: ts.Identifier): boolean {
  const parent = node.parent;
  return ts.isImportClause(parent) || ts.isImportSpecifier(parent) || ts.isNamespaceImport(parent) || ts.isImportEqualsDeclaration(parent);
}

function isDeclarationIdentifier(node: ts.Identifier): boolean {
  const parent = node.parent;
  return (
    isImportedIdentifier(node)
    || (ts.isFunctionDeclaration(parent) && parent.name === node)
    || (ts.isVariableDeclaration(parent) && parent.name === node)
    || (ts.isClassDeclaration(parent) && parent.name === node)
  );
}

function isExportedNode(node: ts.Node): boolean {
  return ts.getCombinedModifierFlags(node as ts.Declaration) & ts.ModifierFlags.Export ? true : false;
}

function countSymbolReferences(checker: ts.TypeChecker, sourceFile: ts.SourceFile): Map<ts.Symbol, number> {
  const usageCounts = new Map<ts.Symbol, number>();

  function visit(node: ts.Node): void {
    if (ts.isIdentifier(node) && !isDeclarationIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      if (symbol) {
        usageCounts.set(symbol, (usageCounts.get(symbol) ?? 0) + 1);
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return usageCounts;
}


function buildUnusedImportCandidates(
  checker: ts.TypeChecker,
  sourceFile: ts.SourceFile,
  root: string,
): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const imports: Array<{ name: string; line: number; symbol: ts.Symbol }> = [];

  function collectImports(node: ts.Node): void {
    if (ts.isImportClause(node) && node.name) {
      const symbol = checker.getSymbolAtLocation(node.name);
      if (symbol) {
        imports.push({
          name: node.name.text,
          line: sourceFile.getLineAndCharacterOfPosition(node.name.getStart()).line + 1,
          symbol,
        });
      }
    }
    if (ts.isNamespaceImport(node)) {
      const symbol = checker.getSymbolAtLocation(node.name);
      if (symbol) {
        imports.push({
          name: node.name.text,
          line: sourceFile.getLineAndCharacterOfPosition(node.name.getStart()).line + 1,
          symbol,
        });
      }
    }
    if (ts.isImportSpecifier(node)) {
      const binding = node.name;
      const symbol = checker.getSymbolAtLocation(binding);
      if (symbol) {
        imports.push({
          name: binding.text,
          line: sourceFile.getLineAndCharacterOfPosition(binding.getStart()).line + 1,
          symbol,
        });
      }
    }
    ts.forEachChild(node, collectImports);
  }

  const usageCounts = countSymbolReferences(checker, sourceFile);
  collectImports(sourceFile);


  return imports
    .filter((binding) => (usageCounts.get(binding.symbol) ?? 0) === 0)
    .map((binding) => ({
      id: `ts-unused-import-${relPath}-${binding.line}-${binding.name}`,
      kind: "unused_import",
      title: `Remove unused import ${binding.name}`,
      description: `Unused TypeScript import \`${binding.name}\` in ${relPath}`,
      language: "typescript",
      scope: "local",
      source: ["static"],
      files: [relPath],
      symbols: [binding.name],
      anchorRegions: [{ file: relPath, startLine: binding.line, endLine: binding.line }],
      estimatedBenefit: { maintainabilityGain: 0.08 },
      estimatedRisk: { semanticRisk: 0.02, conflictRisk: 0.03 },
      estimatedDiff: { filesTouched: 1, linesDeleted: 1, linesModified: 1 },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.9,
      applyModeHint: "auto",
      requiredChecks: ["parse", "lint", "typecheck"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-unused-import"],
        evidence: [`line:${binding.line}`, `symbol:${binding.name}`],
      },
    }));
}

function buildLongFunctionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const totalLines = sourceFile.getLineAndCharacterOfPosition(sourceFile.end).line + 1;
  const candidates: CandidatePayload[] = [];

  function visit(node: ts.Node): void {
    if (ts.isFunctionDeclaration(node) && node.name && node.body) {
      const startLine = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
      const endLine = sourceFile.getLineAndCharacterOfPosition(node.body.end).line + 1;
      const length = endLine - startLine + 1;
      if (length >= LONG_FUNCTION_THRESHOLD) {
        candidates.push({
          id: `ts-extract-function-${relPath}-${startLine}-${node.name.text}`,
          kind: "extract_function",
          title: `Extract logic from long function ${node.name.text}`,
          description: `Function \`${node.name.text}\` in ${relPath} spans ${length} lines and is a candidate for extraction`,
          language: "typescript",
          scope: "local",
          source: ["static", "metric"],
          files: [relPath],
          symbols: [node.name.text],
          anchorRegions: [{ file: relPath, startLine, endLine }],
          estimatedBenefit: {
            complexityReduction: Math.min(1, length / Math.max(totalLines, 1)),
            maintainabilityGain: 0.35,
          },
          estimatedRisk: { semanticRisk: 0.35, testRisk: 0.25, conflictRisk: 0.15 },
          estimatedDiff: {
            filesTouched: 1,
            linesAdded: Math.max(3, Math.floor(length / 4)),
            linesModified: length,
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
            evidence: [`line_span:${length}`, `symbol:${node.name.text}`],
          },
        });
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return candidates;
}

function buildLargeModuleCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
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

function buildDuplicateFunctionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const groups = new Map<string, Array<{ name: string; startLine: number; endLine: number; length: number }>>();

  for (const statement of sourceFile.statements) {
    if (!ts.isFunctionDeclaration(statement) || !statement.name || !statement.body) {
      continue;
    }
    const duplicateKey = duplicateFunctionKey(sourceFile, statement);
    if (!duplicateKey) {
      continue;
    }
    const span = lineSpan(sourceFile, statement);
    const entry = {
      name: statement.name.text,
      startLine: span.startLine,
      endLine: span.endLine,
      length: span.length,
    };
    groups.set(duplicateKey, [...(groups.get(duplicateKey) ?? []), entry]);
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

function buildRemoveAbstractionCandidates(sourceFile: ts.SourceFile, root: string): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const candidates: CandidatePayload[] = [];

  for (const statement of sourceFile.statements) {
    if (!ts.isFunctionDeclaration(statement) || !statement.name || statement.end === undefined) {
      continue;
    }
    if (!statement.name.text.startsWith("_")) {
      continue;
    }
    const target = passthroughTarget(statement);
    if (!target) {
      continue;
    }
    const span = lineSpan(sourceFile, statement);
    candidates.push({
      id: `ts-remove-abstraction-${relPath}-${span.startLine}-${statement.name.text}`,
      kind: "remove_abstraction",
      title: `Inline thin wrapper ${statement.name.text}`,
      description: `Private TypeScript wrapper \`${statement.name.text}\` in ${relPath} only forwards to \`${target}\` and can likely be removed`,
      language: "typescript",
      scope: "module",
      source: ["static", "metric"],
      files: [relPath],
      symbols: [statement.name.text],
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
        evidence: [`symbol:${statement.name.text}`, `target:${target}`, `line_span:${span.length}`],
      },
    });
  }

  return candidates;
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

function buildCycleCandidates(root: string, files: string[], program: ts.Program): CandidatePayload[] {
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

function buildUnusedSymbolCandidates(
  checker: ts.TypeChecker,
  sourceFile: ts.SourceFile,
  root: string,
): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const usageCounts = countSymbolReferences(checker, sourceFile);
  const candidates: CandidatePayload[] = [];

  for (const statement of sourceFile.statements) {
    if (!ts.isFunctionDeclaration(statement) || !statement.name || statement.end === undefined) {
      continue;
    }
    if (isExportedNode(statement)) {
      continue;
    }
    const symbol = checker.getSymbolAtLocation(statement.name);
    if (!symbol || (usageCounts.get(symbol) ?? 0) > 0) {
      continue;
    }
    const startLine = sourceFile.getLineAndCharacterOfPosition(statement.getStart(sourceFile)).line + 1;
    const endLine = sourceFile.getLineAndCharacterOfPosition(statement.end).line + 1;
    const length = endLine - startLine + 1;
    candidates.push({
      id: `ts-unused-symbol-${relPath}-${startLine}-${statement.name.text}`,
      kind: "unused_symbol",
      title: `Remove unused symbol ${statement.name.text}`,
      description: `Top-level TypeScript function \`${statement.name.text}\` in ${relPath} is not referenced`,
      language: "typescript",
      scope: "module",
      source: ["static"],
      files: [relPath],
      symbols: [statement.name.text],
      anchorRegions: [{ file: relPath, startLine, endLine }],
      estimatedBenefit: { maintainabilityGain: 0.18 },
      estimatedRisk: { semanticRisk: 0.08, conflictRisk: 0.04 },
      estimatedDiff: { filesTouched: 1, linesDeleted: length, linesModified: length },
      contextSignals: createEmptyContextSignals(),
      boundaryImpact: createEmptyBoundaryImpact(),
      confidence: 0.86,
      applyModeHint: "auto",
      requiredChecks: ["parse", "lint", "typecheck"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-unused-symbol"],
        evidence: [`line_span:${length}`, `symbol:${statement.name.text}`],
      },
    });
  }

  return candidates;
}


function formatDiagnostic(root: string, diagnostic: ts.Diagnostic): string {
  const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, " ");
  if (!diagnostic.file || diagnostic.start === undefined) {
    return message;
  }
  const { line, character } = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start);
  return `${relativePosix(root, diagnostic.file.fileName)}:${line + 1}:${character + 1} ${message}`;
}

function buildVerificationChecks(root: string, files: string[], program: ts.Program): VerificationCheckPayload[] {
  const syntacticDiagnostics = [
    ...program.getOptionsDiagnostics(),
    ...files.flatMap((fileName) => {
      const sourceFile = program.getSourceFile(fileName);
      return sourceFile ? program.getSyntacticDiagnostics(sourceFile) : [];
    }),
  ];
  const semanticDiagnostics = files.flatMap((fileName) => {
    const sourceFile = program.getSourceFile(fileName);
    return sourceFile ? program.getSemanticDiagnostics(sourceFile) : [];
  });

  return [
    {
      name: "typescript_parse",
      kind: "parse",
      status: syntacticDiagnostics.length === 0 ? "passed" : "failed",
      evidence:
        syntacticDiagnostics.length === 0
          ? [`parsed ${files.length} TypeScript/JavaScript files`]
          : syntacticDiagnostics.slice(0, 20).map((diagnostic) => formatDiagnostic(root, diagnostic)),
      details: { fileCount: files.length, diagnosticCount: syntacticDiagnostics.length },
    },
    {
      name: "typescript_typecheck",
      kind: "typecheck",
      status: semanticDiagnostics.length === 0 ? "passed" : "failed",
      evidence:
        semanticDiagnostics.length === 0
          ? [`typechecked ${files.length} TypeScript/JavaScript files`]
          : semanticDiagnostics.slice(0, 20).map((diagnostic) => formatDiagnostic(root, diagnostic)),
      details: { fileCount: files.length, diagnosticCount: semanticDiagnostics.length },
    },
  ];
}

function scan(rootArg: string): WorkerScanSuccess {
  const { root, files, program } = createProgram(rootArg);
  const checker = program.getTypeChecker();
  const candidates: CandidatePayload[] = [];

  for (const fileName of files) {
    const sourceFile = program.getSourceFile(fileName);
    if (!sourceFile || sourceFile.isDeclarationFile) {
      continue;
    }
    candidates.push(...buildUnusedImportCandidates(checker, sourceFile, root));
    candidates.push(...buildUnusedSymbolCandidates(checker, sourceFile, root));
    candidates.push(...buildLongFunctionCandidates(sourceFile, root));
    candidates.push(...buildLargeModuleCandidates(sourceFile, root));
    candidates.push(...buildDuplicateFunctionCandidates(sourceFile, root));
    candidates.push(...buildRemoveAbstractionCandidates(sourceFile, root));
  }
  candidates.push(...buildCycleCandidates(root, files, program));

  return {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: [...PROTOCOL_CAPABILITIES],
    ok: true,
    command: "scan",
    candidates: sortCandidates(candidates),
  };
}

function verify(rootArg: string): WorkerVerifySuccess {
  const { root, files, program } = createProgram(rootArg);
  return {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: [...PROTOCOL_CAPABILITIES],
    ok: true,
    command: "verify",
    checks: buildVerificationChecks(root, files, program),
  };
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks).toString("utf-8");
}

function failure(command: WorkerCommand, code: string, message: string, details?: Record<string, unknown>): WorkerFailure {
  return {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: [...PROTOCOL_CAPABILITIES],
    ok: false,
    command,
    error: { code, message, details },
  };
}

function parseRequest(payload: string): WorkerRequest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    throw failure("scan", "malformed_json", "Worker request was not valid JSON", {
      error: error instanceof Error ? error.message : String(error),
    });
  }

  if (!parsed || typeof parsed !== "object") {
    throw failure("scan", "invalid_request", "Worker request must be an object");
  }

  const request = parsed as Partial<WorkerRequest>;
  const command = request.command ?? "scan";
  if (request.protocolVersion !== PROTOCOL_VERSION) {
    throw failure(command, "version_mismatch", "Worker protocol version mismatch", {
      expected: PROTOCOL_VERSION,
      actual: request.protocolVersion,
    });
  }
  if (request.command !== "scan" && request.command !== "verify") {
    throw failure(command, "unsupported_command", "Worker command is not supported", {
      command: request.command,
    });
  }
  if (typeof request.root !== "string" || request.root.length === 0) {
    throw failure(command, "invalid_request", "Worker request must include a root path");
  }
  return request as WorkerRequest;
}

async function main(): Promise<void> {
  let response: WorkerResponse;
  let shouldFail = false;

  try {
    const request = parseRequest(await readStdin());
    response = request.command === "scan" ? scan(request.root) : verify(request.root);
  } catch (error) {
    shouldFail = true;
    response = typeof error === "object" && error !== null && "ok" in error
      ? (error as WorkerFailure)
      : failure("scan", "execution_failed", error instanceof Error ? error.message : String(error));
  }

  process.stdout.write(`${JSON.stringify(response)}\n`);
  if (shouldFail) {
    process.exitCode = 1;
  }
}

void main();
