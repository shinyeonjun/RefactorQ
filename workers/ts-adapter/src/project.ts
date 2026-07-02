import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import ts from "typescript";

import type { CandidatePayload } from "./protocol.ts";

const IGNORED = new Set([
  ".git",
  ".gjc",
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

export function relativePosix(root: string, full: string): string {
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

export function createProgram(rootArg: string): { root: string; files: string[]; program: ts.Program } {
  const root = resolve(rootArg);
  const files = collectSourceFiles(root);
  const program = ts.createProgram(files, {
    allowJs: true,
    checkJs: false,
    noEmit: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    esModuleInterop: true,
    allowSyntheticDefaultImports: true,
    jsx: ts.JsxEmit.Preserve,
    skipLibCheck: true,
  });
  return { root, files, program };
}

export function sortCandidates(candidates: CandidatePayload[]): CandidatePayload[] {
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

export function lineSpan(sourceFile: ts.SourceFile, node: ts.Node): { startLine: number; endLine: number; length: number } {
  const startLine = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  const endLine = sourceFile.getLineAndCharacterOfPosition(node.end).line + 1;
  return { startLine, endLine, length: endLine - startLine + 1 };
}
