import ts from "typescript";

import {
  buildDuplicateFunctionCandidates,
  buildInlineFunctionCandidates,
  buildLongFunctionCandidates,
  buildRemoveAbstractionCandidates,
} from "./function-candidates.ts";
import { buildCycleCandidates, buildLayerViolationCandidates } from "./graph.ts";
import { buildLargeModuleCandidates } from "./module-candidates.ts";
import {
  PROTOCOL_CAPABILITIES,
  PROTOCOL_VERSION,
} from "./protocol.ts";
import type {
  CandidatePayload,
  VerificationCheckPayload,
  WorkerCommand,
  WorkerFailure,
  WorkerRequest,
  WorkerResponse,
  WorkerScanSuccess,
  WorkerVerifySuccess,
} from "./protocol.ts";
import { createProgram, relativePosix, sortCandidates } from "./project.ts";
import { buildUnusedImportCandidates, buildUnusedSymbolCandidates } from "./symbol-candidates.ts";

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
    candidates.push(...buildUnusedSymbolCandidates(checker, sourceFile, root, files.length));
    candidates.push(...buildLongFunctionCandidates(sourceFile, root));
    candidates.push(...buildLargeModuleCandidates(sourceFile, root));
    candidates.push(...buildDuplicateFunctionCandidates(sourceFile, root));
    candidates.push(...buildRemoveAbstractionCandidates(sourceFile, root));
    candidates.push(...buildInlineFunctionCandidates(checker, sourceFile, root));
  }
  candidates.push(...buildLayerViolationCandidates(root, files, program));
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
  let command: WorkerCommand = "scan";

  try {
    const request = parseRequest(await readStdin());
    command = request.command;
    response = command === "scan" ? scan(request.root) : verify(request.root);
  } catch (error) {
    shouldFail = true;
    response = typeof error === "object" && error !== null && "ok" in error
      ? (error as WorkerFailure)
      : failure(command, "execution_failed", error instanceof Error ? error.message : String(error));
  }

  process.stdout.write(`${JSON.stringify(response)}\n`);
  if (shouldFail) {
    process.exitCode = 1;
  }
}

void main();
