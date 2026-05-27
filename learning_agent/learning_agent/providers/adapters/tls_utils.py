from __future__ import annotations

import importlib
import os
import ssl
from pathlib import Path


def build_web_tls_context(
    *,
    ca_bundle_path: str | None = None,
    prefer_system_trust_store: bool = True,
) -> ssl.SSLContext:
    bundle_path = _resolve_ca_bundle_path(ca_bundle_path)
    if bundle_path:
        resolved = Path(bundle_path).expanduser()
        if not resolved.is_file():
            raise ValueError(f"CA bundle path does not exist: {resolved}")
        return ssl.create_default_context(cafile=str(resolved))

    if prefer_system_trust_store:
        context = _build_truststore_context()
        if context is not None:
            return context

    return ssl.create_default_context()


def tls_failure_hint() -> str:
    return (
        "Configure web_search.tls_ca_bundle_path (or LA_WEB_SEARCH_CA_BUNDLE_PATH) "
        "for your trusted proxy/root CA, or install truststore to use the system trust store."
    )


def _resolve_ca_bundle_path(explicit_path: str | None) -> str | None:
    for candidate in (
        explicit_path,
        os.getenv("LA_WEB_SEARCH_CA_BUNDLE_PATH"),
        os.getenv("SSL_CERT_FILE"),
        os.getenv("REQUESTS_CA_BUNDLE"),
        os.getenv("CURL_CA_BUNDLE"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def _build_truststore_context() -> ssl.SSLContext | None:
    try:
        truststore = importlib.import_module("truststore")
    except ImportError:
        return None

    ssl_context_cls = getattr(truststore, "SSLContext", None)
    if ssl_context_cls is None:
        return None
    return ssl_context_cls(ssl.PROTOCOL_TLS_CLIENT)


__all__ = [
    "build_web_tls_context",
    "tls_failure_hint",
]
