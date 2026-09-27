"""Enterprise identity and request-security primitives.

The package deliberately contains no network-backed identity provider.  OIDC
configuration is a contract for a future adapter; local development and
service identities remain explicit, separately validated trust paths.
"""

from .authorization import AuthorizationError, AuthorizationService
from .config import EnterpriseIdentityConfig, OIDCConfiguration
from .csrf import CSRFError, CSRFProtector
from .identity import (
    IdentityError,
    PrincipalResolver,
    WindowsDevelopmentIdentityProvider,
)
from .principal import PrincipalContext, PrincipalType
from .service_identity import ServiceIdentityError, ServiceIdentityManager
from .session import EnterpriseSessionManager, SessionError, SessionValidation

__all__ = [
    "AuthorizationError",
    "AuthorizationService",
    "CSRFError",
    "CSRFProtector",
    "EnterpriseIdentityConfig",
    "EnterpriseSessionManager",
    "IdentityError",
    "OIDCConfiguration",
    "PrincipalContext",
    "PrincipalResolver",
    "PrincipalType",
    "ServiceIdentityError",
    "ServiceIdentityManager",
    "SessionError",
    "SessionValidation",
    "WindowsDevelopmentIdentityProvider",
]
