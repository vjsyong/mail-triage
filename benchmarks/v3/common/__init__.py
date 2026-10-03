"""Shared, dependency-free primitives for benchmark v3.

Stdlib only, so every v3 package (build, sandbox, adapters, runner, scoring)
can import these without the application venv or any third-party pin.  These
are namespaced adaptations of the small pure v2 primitives -- v3 never imports
``benchmarks.v2`` at runtime (see ``specs/benchmark-v3.spec.md``).
"""
