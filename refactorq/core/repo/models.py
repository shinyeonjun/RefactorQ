from __future__ import annotations

from pydantic import BaseModel, Field


class RepoManifestMap(BaseModel):
    pyproject: bool = False
    package_json: bool = Field(default=False, alias="packageJson")
    tsconfig: bool = False
    requirements_txt: bool = Field(default=False, alias="requirementsTxt")
    poetry_lock: bool = Field(default=False, alias="poetryLock")
    uv_lock: bool = Field(default=False, alias="uvLock")


class RepoSnapshot(BaseModel):
    root: str
    python_files: int = Field(default=0, alias="pythonFiles")
    typescript_files: int = Field(default=0, alias="typescriptFiles")
    javascript_files: int = Field(default=0, alias="javascriptFiles")
    manifests: RepoManifestMap
    toolchain: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    mixed_language: bool = Field(default=False, alias="mixedLanguage")
    boundary_artifacts: list[str] = Field(default_factory=list, alias="boundaryArtifacts")
