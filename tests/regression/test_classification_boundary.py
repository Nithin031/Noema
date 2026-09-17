"""Classification subsystem boundary tests (Phase 2 extraction).

* Import direction: ``noema.classification`` consumes domain models,
  infrastructure interfaces, configuration, and observability. It must
  never import application orchestration, the daemon lifecycle,
  API/dashboard code, web code, realtime, or intervention logic.
* Repository conformance: ``SQLiteStore`` satisfies every operation of
  the narrow ``ClassificationRepository`` contract with compatible
  signatures, so the SQL can remain infrastructure-owned.
"""

import ast
import importlib.util
import inspect
import pkgutil

import noema.classification as classification_pkg
from noema.classification import repository as repository_mod
from noema.infrastructure.database import SQLiteStore


FORBIDDEN_ROOTS = (
    "noema.application.pipeline",
    "noema.runtime",
    "noema.api",
    "web",
    "noema.application.realtime",
    "noema.domain.intervention",
)


def _module_imports(modname):
    spec = importlib.util.find_spec(modname)
    assert spec is not None and spec.origin, modname
    with open(spec.origin, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _classification_modules():
    prefix = classification_pkg.__name__ + "."
    return sorted(
        prefix + info.name for info in pkgutil.iter_modules(classification_pkg.__path__)
    )


def test_classification_import_direction():
    """No classification module may depend on orchestration/lifecycle/UI."""
    violations = []
    for modname in _classification_modules():
        for imported in _module_imports(modname):
            if imported in FORBIDDEN_ROOTS or any(
                imported.startswith(root + ".") for root in FORBIDDEN_ROOTS
            ):
                violations.append("{} imports {}".format(modname, imported))
    assert violations == []


def test_sqlite_store_satisfies_classification_repository():
    """The infrastructure store implements every contract operation."""
    proto_params = {}
    for name in dir(repository_mod.ClassificationRepository):
        if name.startswith("_"):
            continue
        member = inspect.getattr_static(repository_mod.ClassificationRepository, name)
        if callable(member):
            proto_params[name] = list(inspect.signature(member).parameters)[1:]
    assert proto_params, "contract exposes no operations"
    for name, params in sorted(proto_params.items()):
        assert callable(getattr(SQLiteStore, name, None)), name
        impl_params = list(inspect.signature(getattr(SQLiteStore, name)).parameters)[1:]
        assert impl_params == params, (name, impl_params, params)
    assert isinstance(SQLiteStore(), repository_mod.ClassificationRepository)
