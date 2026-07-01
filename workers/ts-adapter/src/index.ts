import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";

const PROTOCOL_VERSION = 1;
const PROTOCOL_CAPABILITIES = ["scan", "deterministic-ordering", "typescript-semantic-candidates"];
const LONG_FUNCTION_THRESHOLD = 40;
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


type WorkerRequest = {
  protocolVersion: number;
  capabilities?: string[];
  command: "scan";
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
  error: WorkerError;
};

type WorkerSuccess = {
  protocolVersion: number;
  capabilities: string[];
  ok: true;
  candidates: CandidatePayload[];
};

type WorkerResponse = WorkerFailure | WorkerSuccess;

type CandidatePayload = {
  id: string;
  kind: "unused_import" | "extract_function";
  title: string;
  description: string;
  language: "typescript";
  scope: "local";
  source: Array<"static" | "metric">;
  files: string[];
  symbols: string[];
  anchorRegions: Array<{ file: string; startLine: number; endLine: number }>;
  estimatedBenefit: {
    complexityReduction?: number;
    maintainabilityGain?: number;
  };
  estimatedRisk: {
    semanticRisk: number;
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
  applyModeHint: "auto" | "guarded";
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

function isImportedIdentifier(node: ts.Identifier): boolean {
  const parent = node.parent;
  return ts.isImportClause(parent) || ts.isImportSpecifier(parent) || ts.isNamespaceImport(parent) || ts.isImportEqualsDeclaration(parent);
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

  const usageCounts = new Map<ts.Symbol, number>();
  function collectUsages(node: ts.Node): void {
    if (ts.isIdentifier(node) && !isImportedIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      if (symbol) {
        usageCounts.set(symbol, (usageCounts.get(symbol) ?? 0) + 1);
      }
    }
    ts.forEachChild(node, collectUsages);
  }

  collectImports(sourceFile);
  collectUsages(sourceFile);

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

function scan(rootArg: string): WorkerSuccess {
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
  const checker = program.getTypeChecker();
  const candidates: CandidatePayload[] = [];

  for (const fileName of files) {
    const sourceFile = program.getSourceFile(fileName);
    if (!sourceFile || sourceFile.isDeclarationFile) {
      continue;
    }
    candidates.push(...buildUnusedImportCandidates(checker, sourceFile, root));
    candidates.push(...buildLongFunctionCandidates(sourceFile, root));
  }

  return {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: [...PROTOCOL_CAPABILITIES],
    ok: true,
    candidates: sortCandidates(candidates),
  };
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks).toString("utf-8");
}

function failure(code: string, message: string, details?: Record<string, unknown>): WorkerFailure {
  return {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: [...PROTOCOL_CAPABILITIES],
    ok: false,
    error: { code, message, details },
  };
}

function parseRequest(payload: string): WorkerRequest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    throw failure("malformed_json", "Worker request was not valid JSON", {
      error: error instanceof Error ? error.message : String(error),
    });
  }

  if (!parsed || typeof parsed !== "object") {
    throw failure("invalid_request", "Worker request must be an object");
  }

  const request = parsed as Partial<WorkerRequest>;
  if (request.protocolVersion !== PROTOCOL_VERSION) {
    throw failure("version_mismatch", "Worker protocol version mismatch", {
      expected: PROTOCOL_VERSION,
      actual: request.protocolVersion,
    });
  }
  if (request.command !== "scan") {
    throw failure("unsupported_command", "Worker command is not supported", {
      command: request.command,
    });
  }
  if (typeof request.root !== "string" || request.root.length === 0) {
    throw failure("invalid_request", "Worker request must include a root path");
  }
  return request as WorkerRequest;
}

async function main(): Promise<void> {
  let response: WorkerResponse;
  let shouldFail = false;

  try {
    const request = parseRequest(await readStdin());
    response = scan(request.root);
  } catch (error) {
    shouldFail = true;
    response = typeof error === "object" && error !== null && "ok" in error
      ? (error as WorkerFailure)
      : failure("scan_failed", error instanceof Error ? error.message : String(error));
  }

  process.stdout.write(`${JSON.stringify(response)}\n`);
  if (shouldFail) {
    process.exitCode = 1;
  }
}

void main();
