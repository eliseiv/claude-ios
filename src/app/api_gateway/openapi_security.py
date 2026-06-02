"""OpenAPI JWT Bearer security scheme reflection (08-api-documentation.md, R2).

Declares an HTTP Bearer (JWT) security scheme so Swagger UI shows the lock icon on
protected `/v1/*` endpoints and the Authorize button works (enter `Bearer <JWT>` once).

This is documentation only: actual JWT verification stays in `app.api_gateway.auth` /
`app.deps.get_current_user`. `auto_error=False` ensures this dependency never raises and
never short-circuits the real check — it only contributes the security scheme to OpenAPI.
"""

from __future__ import annotations

from fastapi.security import HTTPBearer

# scheme_name fixes the OpenAPI components.securitySchemes key to `bearerAuth`.
# bearerFormat=JWT documents the token shape; description (RU) explains the auth model.
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать "
        "с `sub`, иначе `403`. Введите токен как `Bearer <JWT>` через кнопку Authorize — "
        "он применится ко всем защищённым вызовам. Реальная проверка подписи/exp/iss/aud "
        "выполняется на сервере; это объявление — только для клиента и Swagger UI."
    ),
)
