"""The cross-repo pin contract, made loud.

pyproject's adcp pin is not an ordinary dependency choice. Its stated job is to hold the
buyer on the SAME AdCP spec version as the salesagent it drives, so the differential engine
compares like with like. Nothing enforced that, and it drifted: the buyer sat on 5.7.0
(spec 3.1.0-beta.3) after salesagent moved to 6.6.0 (spec 3.1.1), while the comment above
the pin still claimed the two matched.

Three checks, deliberately layered so each can fail alone and say something different:

1. the installed SDK matches the pin  -- catches a stale venv, always runnable
2. the spec version matches a literal -- forces a bump to be a deliberate edit here
3. the seller agrees                  -- checked against a salesagent checkout if one is
                                         present, and against a live seller if one is up
"""

from __future__ import annotations

import os
import pathlib
import re
import tomllib

import adcp
import httpx
import pytest

#: The AdCP spec version this repo targets. Changing this is the bump; it should move in
#: the same commit as the pyproject pin and for the same reason.
EXPECTED_SPEC_VERSION = "3.1.1"
EXPECTED_SDK_VERSION = "6.6.0"

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pinned_adcp() -> str:
    deps = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    for dep in deps:
        m = re.fullmatch(r"adcp==([0-9][^\s,;]*)", dep.strip())
        if m:
            return m.group(1)
    raise AssertionError("pyproject declares no exact adcp== pin")


def test_pyproject_pins_adcp_exactly():
    """A range would let the two repos drift apart without any edit here."""
    assert _pinned_adcp() == EXPECTED_SDK_VERSION


def test_installed_sdk_matches_the_pin():
    assert adcp.get_adcp_sdk_version() == _pinned_adcp(), (
        "the installed adcp does not match pyproject -- run `uv sync --extra dev`"
    )


def test_installed_sdk_targets_the_expected_spec():
    assert adcp.get_adcp_spec_version() == EXPECTED_SPEC_VERSION


def test_rfc8785_is_a_declared_dependency_not_an_adcp_transitive():
    """core/idempotency.py imports it directly, and it is on the money path.

    It also arrives as an adcp dependency, so this passed by luck until adcp was bumped.
    A direct import is a direct dependency.
    """
    deps = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    assert any(d.strip().startswith("rfc8785") for d in deps)


def _salesagent_root() -> pathlib.Path | None:
    env = os.environ.get("SALESAGENT_ROOT")
    candidates = [pathlib.Path(env)] if env else []
    candidates.append(_ROOT.parent / "salesagent")
    for c in candidates:
        if (c / "pyproject.toml").is_file():
            return c
    return None


def test_the_seller_checkout_pins_the_same_adcp():
    """Skipped when no salesagent checkout is next to us; that is the common CI case."""
    root = _salesagent_root()
    if root is None:
        pytest.skip("no salesagent checkout found (set SALESAGENT_ROOT)")
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"adcp==([0-9][^"]*)"', text)
    assert m, f"{root}/pyproject.toml declares no exact adcp== pin"
    assert m.group(1) == _pinned_adcp(), (
        f"pin drift: buyer targets {_pinned_adcp()}, salesagent at {root} targets {m.group(1)}"
    )


def test_a_live_seller_advertises_a_major_version_we_target():
    """The wire only carries release precision, so this is a coarse check -- but a real one:
    a seller that dropped support for our major version would make every comparison moot."""
    base = os.environ.get("SELLER_BASE", "http://localhost:8092")
    try:
        resp = httpx.get(
            f"{base}/api/v1/capabilities",
            headers={
                "x-adcp-auth": os.environ.get("SELLER_TOKEN", "ci-test-token"),
                "x-adcp-tenant": os.environ.get("SELLER_TENANT", "ci-test"),
            },
            timeout=5.0,
        )
    except Exception:
        pytest.skip(f"no salesagent reachable at {base}")
    if resp.status_code != 200:
        pytest.skip(f"capabilities returned {resp.status_code}")

    advertised = (resp.json().get("adcp") or {}).get("major_versions") or []
    ours = int(EXPECTED_SPEC_VERSION.split(".")[0])
    assert ours in advertised, f"seller advertises {advertised}, we target major {ours}"


def test_a_live_seller_still_supports_idempotency_replay():
    """The durable executor's exactly-once story assumes the seller replays a repeated key.

    If a seller ever advertises idempotency.supported=false, the buyer's guarantee reduces to
    whatever DBOS can do in-process -- which does not survive a fresh process. Better to fail
    here than to discover it by double-booking.
    """
    base = os.environ.get("SELLER_BASE", "http://localhost:8092")
    try:
        resp = httpx.get(
            f"{base}/api/v1/capabilities",
            headers={
                "x-adcp-auth": os.environ.get("SELLER_TOKEN", "ci-test-token"),
                "x-adcp-tenant": os.environ.get("SELLER_TENANT", "ci-test"),
            },
            timeout=5.0,
        )
    except Exception:
        pytest.skip(f"no salesagent reachable at {base}")
    if resp.status_code != 200:
        pytest.skip(f"capabilities returned {resp.status_code}")

    idem = (resp.json().get("adcp") or {}).get("idempotency") or {}
    assert idem.get("supported") is True, f"seller does not advertise idempotency: {idem}"
