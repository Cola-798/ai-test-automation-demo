from __future__ import annotations

from dataclasses import dataclass

from .schemas import ExecutionPlan, StateMode


@dataclass(frozen=True)
class StateConflict:
    left_case_id: str
    right_case_id: str
    domains: tuple[str, ...]
    message: str


def state_conflicts(plans: list[ExecutionPlan]) -> list[StateConflict]:
    """Find declared read/write overlaps; legacy plans are intentionally ignored."""
    conflicts: list[StateConflict] = []
    for index, left in enumerate(plans):
        if left.state_contract.mode == StateMode.LEGACY_UNMANAGED:
            continue
        left_requires = {item.domain for item in left.state_contract.requires}
        left_mutates = set(left.state_contract.mutates)
        for right in plans[index + 1 :]:
            if right.state_contract.mode == StateMode.LEGACY_UNMANAGED:
                continue
            right_requires = {item.domain for item in right.state_contract.requires}
            right_mutates = set(right.state_contract.mutates)
            overlap = sorted((left_mutates & right_requires) | (right_mutates & left_requires) | (left_mutates & right_mutates))
            if not overlap:
                continue
            left_has_recovery = _has_verified_recovery(left)
            right_has_recovery = _has_verified_recovery(right)
            if left_has_recovery and right_has_recovery:
                continue
            conflicts.append(
                StateConflict(
                    left_case_id=left.source.case_id,
                    right_case_id=right.source.case_id,
                    domains=tuple(overlap),
                    message=(
                        f"{left.source.case_id} 与 {right.source.case_id} 共享可变数据域 "
                        f"{', '.join(overlap)}，但没有声明双方都能在执行后恢复；不能仅依赖批次执行顺序。"
                    ),
                )
            )
    return conflicts


def _has_verified_recovery(plan: ExecutionPlan) -> bool:
    contract = plan.state_contract
    if contract.restore.value != "always" or not contract.restore_steps or not contract.restore_oracles:
        return False
    if contract.mode != StateMode.DESTRUCTIVE:
        return True
    keyed_created = {
        step.metadata.get("created_object_key"): step.metadata.get("creates_object_identity")
        for step in [
            *plan.steps,
            *(step for requirement in contract.requires for step in requirement.prepare_steps),
        ]
        if step.metadata.get("created_object_key") and step.metadata.get("creates_object_identity")
    }
    if keyed_created:
        keyed_restore = {step.metadata.get("owns_object_key"): step for step in contract.restore_steps
                         if step.metadata.get("owns_object_key")}
        return set(keyed_created) == set(keyed_restore) and all(
            keyed_restore[key].metadata.get("owns_object_identity") == identity
            and keyed_restore[key].metadata.get("owned_object_expected_text") == identity
            and keyed_restore[key].metadata.get("owned_object_target_key")
            for key, identity in keyed_created.items()
        )
    created = {
        step.metadata.get("creates_object_identity")
        for step in [
            *plan.steps,
            *(step for requirement in contract.requires for step in requirement.prepare_steps),
        ]
        if isinstance(step.metadata.get("creates_object_identity"), str)
        and step.metadata.get("creates_object_identity").strip()
    }
    restore_identities = {
        step.metadata.get("owns_object_identity")
        for step in contract.restore_steps
        if isinstance(step.metadata.get("owns_object_identity"), str)
        and step.metadata.get("owns_object_identity").strip()
    }
    return bool(created) and created == restore_identities and all(
        any(
            step.metadata.get("owns_object_identity") == identity
            and step.metadata.get("owned_object_target_key")
            and step.metadata.get("owned_object_expected_text") == identity
            for step in contract.restore_steps
        )
        for identity in created
    )
