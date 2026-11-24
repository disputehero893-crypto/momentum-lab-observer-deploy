"""Explicit opt-in wiring; default observer and legacy runtime are unchanged."""
from dataclasses import replace
from datetime import datetime, timezone

from .autopilot import PaperAutopilot
from .customer_cloud import SupabaseCustomerCloud, canonical_uuid
from .customer_gateway import CustomerGateway, valid_origin
from .customer_settings import compile_paper_settings


def build_customer_paper_runtime(base, *, account_id, publishable_key,
        expected_settings_version, reviewed_strategy_ids, clock=None, cloud_factory=SupabaseCustomerCloud):
    """Build but do not start a single-owner worker and its authenticated gateway.

    Registry and exact configuration approval are operator-controlled, not AI
    promotion or inferred approval. A saved configuration never arms execution.
    """
    canonical_uuid(account_id)
    canonical_uuid(expected_settings_version)
    if base.mode != "paper" or not base.control_token or not valid_origin(base.cors_origin):
        raise ValueError("customer_runtime_requires_paper_auth_and_https_origin")
    clock = clock or (lambda: datetime.now(timezone.utc))
    cloud = cloud_factory(project_url=base.supabase_url, publishable_key=publishable_key,
        server_key=base.supabase_service_role_key, owner_id=base.owner_user_id, clock=clock)
    row = cloud.settings_for(base.owner_user_id)
    if row["version"] != expected_settings_version:
        raise ValueError("operator_settings_version_mismatch")
    config, binding = compile_paper_settings(base, row, verified_owner_id=base.owner_user_id,
        reviewed_strategy_ids=reviewed_strategy_ids, now=clock())
    # Retain pre-existing independent approval only when it covers these EXACT effective limits.
    if base.paper_enabled and base.paper_reviewed_config_hash == config.review_hash():
        config = replace(config, paper_enabled=True, paper_reviewed_config_hash=base.paper_reviewed_config_hash)
    worker = PaperAutopilot(config, account_id=account_id, customer_binding=binding,
        settings_loader=cloud.settings_for, clock=clock)
    return config, worker, CustomerGateway(worker, cloud, origin=config.cors_origin)
