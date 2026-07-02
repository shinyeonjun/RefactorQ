import ts from "typescript";

export type TopLevelFunctionLike = {
  nameNode: ts.Identifier;
  declaration: ts.Statement;
  functionNode: ts.FunctionDeclaration | ts.FunctionExpression | ts.ArrowFunction;
};

export function collectTopLevelFunctionLikes(sourceFile: ts.SourceFile): TopLevelFunctionLike[] {
  const entries: TopLevelFunctionLike[] = [];
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name && statement.body) {
      entries.push({ nameNode: statement.name, declaration: statement, functionNode: statement });
      continue;
    }
    if (!ts.isVariableStatement(statement) || !(statement.declarationList.flags & ts.NodeFlags.Const)) {
      continue;
    }
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || !declaration.initializer) {
        continue;
      }
      if (ts.isArrowFunction(declaration.initializer) || ts.isFunctionExpression(declaration.initializer)) {
        entries.push({
          nameNode: declaration.name,
          declaration: statement,
          functionNode: declaration.initializer,
        });
      }
    }
  }
  return entries;
}

export function duplicateFunctionKey(sourceFile: ts.SourceFile, entry: TopLevelFunctionLike): string | null {
  const functionBody = entry.functionNode.body;
  if (!functionBody) {
    return null;
  }
  const parameters = entry.functionNode.parameters.map((parameter) => parameter.getText(sourceFile).replace(/\s+/g, " ").trim()).join(",");
  const body = ts.isBlock(functionBody)
    ? functionBody.statements.map((statement) => statement.getText(sourceFile).replace(/\s+/g, " ").trim()).join(";")
    : functionBody.getText(sourceFile).replace(/\s+/g, " ").trim();
  if (!body) {
    return null;
  }
  return `${parameters}|${body}`;
}

export function passthroughTarget(entry: TopLevelFunctionLike): string | null {
  if (isExportedNode(entry.declaration)) {
    return null;
  }
  const functionBody = entry.functionNode.body;
  if (!functionBody) {
    return null;
  }
  let expression: ts.Expression;
  if (ts.isBlock(functionBody)) {
    if (functionBody.statements.length !== 1) {
      return null;
    }
    const [statement] = functionBody.statements;
    if (!statement || !ts.isReturnStatement(statement) || !statement.expression) {
      return null;
    }
    expression = statement.expression;
  } else {
    expression = functionBody;
  }
  if (ts.isParenthesizedExpression(expression)) {
    expression = expression.expression;
  }
  if (ts.isAwaitExpression(expression)) {
    expression = expression.expression;
  }
  if (!ts.isCallExpression(expression)) {
    return null;
  }
  if (!ts.isIdentifier(expression.expression) || expression.expression.text === entry.nameNode.text) {
    return null;
  }
  if (expression.arguments.length !== entry.functionNode.parameters.length) {
    return null;
  }
  const parameterNames = entry.functionNode.parameters.map((parameter) => ts.isIdentifier(parameter.name) ? parameter.name.text : null);
  if (parameterNames.some((name) => name === null)) {
    return null;
  }
  const forwardedNames = expression.arguments.map((argument) => ts.isIdentifier(argument) ? argument.text : null);
  if (forwardedNames.some((name) => name === null)) {
    return null;
  }
  if (parameterNames.join(",") !== forwardedNames.join(",")) {
    return null;
  }
  return expression.expression.text;
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

export function isExportedNode(node: ts.Node): boolean {
  return ts.getCombinedModifierFlags(node as ts.Declaration) & ts.ModifierFlags.Export ? true : false;
}

export function countSymbolReferences(checker: ts.TypeChecker, sourceFile: ts.SourceFile): Map<ts.Symbol, number> {
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
