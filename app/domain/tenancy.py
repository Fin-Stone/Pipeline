"""Who the pipeline is acting for.

Every ledger write happens on behalf of a tenant, and usually on behalf of a
member within it. `TenantContext` is that answer, resolved once at the edge and
threaded through explicitly.

Explicitly, rather than as a thread-local or an implicit default, on purpose:
a required argument makes forgetting the tenant a `TypeError` at the call site,
where an implicit default would make it a silent cross-tenant read. For a
system holding several households' financial records, the noisy failure is the
only acceptable one.

The system runs single-tenant today. `TenantContext` still exists and is still
required, so that switching it on later is a change of how the context is
*resolved* — from configuration to an authenticated session — and not a change
to every call site in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Roles, mirroring app/storage/schema.py. `owner` administers the tenant;
#: `adult` sees what is shared with them; `child` and `viewer` are read-only.
ROLE_OWNER = "owner"
ROLE_ADULT = "adult"
ROLE_CHILD = "child"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_OWNER, ROLE_ADULT, ROLE_CHILD, ROLE_VIEWER)

READ_ONLY_ROLES = (ROLE_CHILD, ROLE_VIEWER)


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The tenant, and optionally the member, a unit of work belongs to."""

    tenant_id: int
    member_id: int | None = None
    role: str = ROLE_OWNER

    @property
    def can_write(self) -> bool:
        return self.role not in READ_ONLY_ROLES


@dataclass(frozen=True, slots=True)
class MemberIdentity:
    """An SSO identity, as an identity provider presents it.

    `issuer` and `subject` are the OIDC `iss` and `sub` claims, which together
    are the only globally stable identifier a provider gives you. Email is
    deliberately not an identity: it can be reassigned to a different person.

    No password ever appears here. Authentication belongs to the identity
    provider, and a credential this system never holds is one it can never leak.
    """

    issuer: str
    subject: str
    email: str | None = None
    display_name: str | None = None
