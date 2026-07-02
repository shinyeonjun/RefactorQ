import ts from "typescript";

import { createEmptyBoundaryImpact, createEmptyContextSignals } from "./protocol.ts";
import type { CandidatePayload } from "./protocol.ts";
import { relativePosix } from "./project.ts";
import { countSymbolReferences, isExportedNode } from "./syntax.ts";

function unusedImportApplyMode(sourceFile: ts.SourceFile, declaration: ts.ImportDeclaration, bindingLine: number): "auto" | "report_only" {
  const startLine = sourceFile.getLineAndCharacterOfPosition(declaration.getStart(sourceFile)).line + 1;
  const endLine = sourceFile.getLineAndCharacterOfPosition(declaration.end).line + 1;
  if (startLine !== endLine || bindingLine !== startLine) {
    return "report_only";
  }
  const rawLine = sourceFile.text.split(/\r?\n/)[startLine - 1] ?? "";
  const trimmed = rawLine.trim();
  if (!trimmed.startsWith("import ") || trimmed.includes("/*") || trimmed.includes("*/") || trimmed.includes("//") || trimmed.includes("(") || trimmed.includes(")") || trimmed.includes("\\")) {
    return "report_only";
  }
  return "auto";
}

export function buildUnusedImportCandidates(
  checker: ts.TypeChecker,
  sourceFile: ts.SourceFile,
  root: string,
): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  const imports: Array<{ name: string; line: number; symbol: ts.Symbol; declaration: ts.ImportDeclaration }> = [];

  function collectImports(node: ts.Node, currentImport: ts.ImportDeclaration | null = null): void {
    const activeImport = ts.isImportDeclaration(node) ? node : currentImport;
    if (ts.isImportClause(node) && node.name && activeImport) {
      const symbol = checker.getSymbolAtLocation(node.name);
      if (symbol) {
        imports.push({
          name: node.name.text,
          line: sourceFile.getLineAndCharacterOfPosition(node.name.getStart()).line + 1,
          symbol,
          declaration: activeImport,
        });
      }
    }
    if (ts.isNamespaceImport(node) && activeImport) {
      const symbol = checker.getSymbolAtLocation(node.name);
      if (symbol) {
        imports.push({
          name: node.name.text,
          line: sourceFile.getLineAndCharacterOfPosition(node.name.getStart()).line + 1,
          symbol,
          declaration: activeImport,
        });
      }
    }
    if (ts.isImportSpecifier(node) && activeImport) {
      const binding = node.name;
      const symbol = checker.getSymbolAtLocation(binding);
      if (symbol) {
        imports.push({
          name: binding.text,
          line: sourceFile.getLineAndCharacterOfPosition(binding.getStart()).line + 1,
          symbol,
          declaration: activeImport,
        });
      }
    }
    ts.forEachChild(node, (child) => collectImports(child, activeImport));
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
      applyModeHint: unusedImportApplyMode(sourceFile, binding.declaration, binding.line),
      requiredChecks: ["parse", "lint", "typecheck"],
      dependencies: [],
      conflicts: [],
      provenance: {
        detectors: ["ts-worker-unused-import"],
        evidence: [`line:${binding.line}`, `symbol:${binding.name}`],
      },
    }));
}

function isSideEffectFreeInitializer(node: ts.Expression | undefined): boolean {
  if (!node) {
    return true;
  }
  if (
    ts.isLiteralExpression(node)
    || node.kind === ts.SyntaxKind.TrueKeyword
    || node.kind === ts.SyntaxKind.FalseKeyword
    || node.kind === ts.SyntaxKind.NullKeyword
    || node.kind === ts.SyntaxKind.UndefinedKeyword
    || ts.isNoSubstitutionTemplateLiteral(node)
    || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isClassExpression(node)
  ) {
    return true;
  }
  if (ts.isIdentifier(node)) {
    return node.text === "undefined";
  }
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isSatisfiesExpression(node)) {
    return isSideEffectFreeInitializer(node.expression);
  }
  if (ts.isArrayLiteralExpression(node)) {
    return node.elements.every((element) => !ts.isSpreadElement(element) && (ts.isOmittedExpression(element) || isSideEffectFreeInitializer(element)));
  }
  if (ts.isObjectLiteralExpression(node)) {
    return node.properties.every((property) => {
      if (ts.isPropertyAssignment(property)) {
        return isSideEffectFreeInitializer(property.initializer);
      }
      if (ts.isMethodDeclaration(property) || ts.isGetAccessorDeclaration(property) || ts.isSetAccessorDeclaration(property)) {
        return true;
      }
      return false;
    });
  }
  return false;
}

function pushUnusedSymbolCandidate(
  candidates: CandidatePayload[],
  args: {
    relPath: string;
    startLine: number;
    endLine: number;
    symbol: string;
    description: string;
  },
): void {
  const { relPath, startLine, endLine, symbol, description } = args;
  const length = endLine - startLine + 1;
  candidates.push({
    id: `ts-unused-symbol-${relPath}-${startLine}-${symbol}`,
    kind: "unused_symbol",
    title: `Remove unused symbol ${symbol}`,
    description,
    language: "typescript",
    scope: "module",
    source: ["static"],
    files: [relPath],
    symbols: [symbol],
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
      evidence: [`line_span:${length}`, `symbol:${symbol}`],
    },
  });
}

function hasModuleSyntax(sourceFile: ts.SourceFile): boolean {
  return sourceFile.statements.some((statement) =>
    ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement) || ts.isExportAssignment(statement)
    || (ts.canHaveModifiers(statement) && !!ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword))
  );
}

export function buildUnusedSymbolCandidates(
  checker: ts.TypeChecker,
  sourceFile: ts.SourceFile,
  root: string,
  projectFileCount: number,
): CandidatePayload[] {
  const relPath = relativePosix(root, sourceFile.fileName);
  if (!hasModuleSyntax(sourceFile) && projectFileCount > 1) {
    return [];
  }
  const usageCounts = countSymbolReferences(checker, sourceFile);
  const candidates: CandidatePayload[] = [];

  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name && statement.end !== undefined) {
      if (isExportedNode(statement)) {
        continue;
      }
      const symbol = checker.getSymbolAtLocation(statement.name);
      if (!symbol || (usageCounts.get(symbol) ?? 0) > 0) {
        continue;
      }
      const startLine = sourceFile.getLineAndCharacterOfPosition(statement.getStart(sourceFile)).line + 1;
      const endLine = sourceFile.getLineAndCharacterOfPosition(statement.end).line + 1;
      pushUnusedSymbolCandidate(candidates, {
        relPath,
        startLine,
        endLine,
        symbol: statement.name.text,
        description: `Top-level TypeScript function \`${statement.name.text}\` in ${relPath} is not referenced`,
      });
    }

    if (ts.isClassDeclaration(statement) && statement.name && statement.end !== undefined) {
      if (isExportedNode(statement)) {
        continue;
      }
      const symbol = checker.getSymbolAtLocation(statement.name);
      if (!symbol || (usageCounts.get(symbol) ?? 0) > 0) {
        continue;
      }
      const startLine = sourceFile.getLineAndCharacterOfPosition(statement.getStart(sourceFile)).line + 1;
      const endLine = sourceFile.getLineAndCharacterOfPosition(statement.end).line + 1;
      pushUnusedSymbolCandidate(candidates, {
        relPath,
        startLine,
        endLine,
        symbol: statement.name.text,
        description: `Top-level TypeScript class \`${statement.name.text}\` in ${relPath} is not referenced`,
      });
    }

    if (ts.isVariableStatement(statement) && !isExportedNode(statement) && statement.declarationList.declarations.length === 1) {
      const declaration = statement.declarationList.declarations[0];
      if (!ts.isIdentifier(declaration.name) || !isSideEffectFreeInitializer(declaration.initializer)) {
        continue;
      }
      const symbol = checker.getSymbolAtLocation(declaration.name);
      if (!symbol || (usageCounts.get(symbol) ?? 0) > 0) {
        continue;
      }
      const startLine = sourceFile.getLineAndCharacterOfPosition(statement.getStart(sourceFile)).line + 1;
      const endLine = sourceFile.getLineAndCharacterOfPosition(statement.end).line + 1;
      pushUnusedSymbolCandidate(candidates, {
        relPath,
        startLine,
        endLine,
        symbol: declaration.name.text,
        description: `Top-level TypeScript variable \`${declaration.name.text}\` in ${relPath} is not referenced`,
      });
    }

  }

  return candidates;
}
