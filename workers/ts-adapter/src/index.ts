import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";

const PROTOCOL_VERSION = 1;
const PROTOCOL_CAPABILITIES = ["scan", "verify", "deterministic-ordering", "typescript-semantic-candidates"];
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
  kind: "unused_import" | "unused_symbol" | "extract_function";

  title: string;
  description: string;
  language: "typescript";
  scope: "local" | "module";
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
  }

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
