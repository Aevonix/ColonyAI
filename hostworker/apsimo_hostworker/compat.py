"""Legacy import names resolve to the canonical module, including its state."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, canonical, spec):
        self.canonical = canonical
        self.canonical_spec = spec

    def create_module(self, spec):
        return importlib.import_module(self.canonical)

    def exec_module(self, module):
        # module_from_spec assigns the alias spec to the returned module.
        # Restore its real identity rather than executing its source again.
        module.__spec__ = self.canonical_spec
        module.__loader__ = self.canonical_spec.loader

    def get_code(self, fullname):
        # runpy uses get_code for `python -m legacy_package.entrypoint`.
        return self.canonical_spec.loader.get_code(self.canonical)

    def is_package(self, fullname):
        return self.canonical_spec.submodule_search_locations is not None


class _AliasFinder(importlib.abc.MetaPathFinder):
    def __init__(self, legacy, canonical):
        self.legacy, self.canonical = legacy, canonical

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.legacy + '.'):
            return None
        canonical = self.canonical + fullname[len(self.legacy):]
        spec = importlib.util.find_spec(canonical)
        if spec is None:
            return None
        loader = _AliasLoader(canonical, spec)
        return importlib.util.spec_from_loader(fullname, loader,
            origin=spec.origin, is_package=spec.submodule_search_locations is not None)


def register_module_alias(legacy: str, canonical: str) -> None:
    """Install a lazy alias once, without eagerly importing optional subsystems."""
    if not any(getattr(finder, 'legacy', None) == legacy
               and getattr(finder, 'canonical', None) == canonical for finder in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder(legacy, canonical))
    sys.modules[legacy] = importlib.import_module(canonical)
