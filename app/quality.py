"""Data-quality checks enforced on a simulated day (per spec)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.engines.hybrid import BlockResult
from app.engines.spec import PlantSpec

EPS = 1e-6


@dataclass
class QualityReport:
    status: str  # OK / PARTIAL / FAILED
    issues: list[str] = field(default_factory=list)
    interpolated_blocks: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def check_day(spec: PlantSpec, results: list[BlockResult]) -> QualityReport:
    issues: list[str] = []
    critical = False

    # Exactly 96 blocks.
    if len(results) != 96:
        issues.append(f"expected 96 blocks, got {len(results)}")
        critical = True

    block_nos = [r.block_no for r in results]
    # No duplicates.
    if len(set(block_nos)) != len(block_nos):
        issues.append("duplicate block numbers detected")
        critical = True
    # No missing blocks.
    if set(block_nos) != set(range(1, 97)):
        missing = sorted(set(range(1, 97)) - set(block_nos))
        if missing:
            issues.append(f"missing blocks: {missing[:5]}{'...' if len(missing) > 5 else ''}")
            critical = True

    # No duplicate timestamps.
    starts = [r.block_start for r in results]
    if len(set(starts)) != len(starts):
        issues.append("duplicate block timestamps detected")
        critical = True

    solar_cap = spec.solar_ac_mw + EPS
    wind_cap = spec.wind_ac_mw + EPS
    interpolated = 0
    for r in results:
        if r.interpolated:
            interpolated += 1
        # No negatives.
        if r.solar_mw < -EPS or r.wind_mw < -EPS or r.total_mw < -EPS:
            issues.append(f"negative generation at block {r.block_no}")
            critical = True
        # Solar zero at night.
        if r.solar_status == "NIGHT" and abs(r.solar_mw) > EPS:
            issues.append(f"solar non-zero at night block {r.block_no}")
            critical = True
        # Caps respected.
        if r.solar_mw > solar_cap:
            issues.append(f"solar_mw>{spec.solar_ac_mw} at block {r.block_no}")
            critical = True
        if r.wind_mw > wind_cap:
            issues.append(f"wind_mw>{spec.wind_ac_mw} at block {r.block_no}")
            critical = True
        # Totals reconcile.
        if abs(r.total_mw - (r.solar_mw + r.wind_mw)) > 1e-4:
            issues.append(f"total_mw != solar+wind at block {r.block_no}")
            critical = True
        # CUF sanity.
        if r.hybrid_cuf > 1.0 + 1e-3:
            issues.append(f"hybrid_cuf>1 at block {r.block_no}")
            critical = True

    if critical:
        status = "FAILED"
    elif interpolated > 0:
        status = "PARTIAL"
    else:
        status = "OK"
    return QualityReport(status=status, issues=issues, interpolated_blocks=interpolated)
