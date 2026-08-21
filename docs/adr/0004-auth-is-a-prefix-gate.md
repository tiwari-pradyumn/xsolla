# Authentication is an ASGI gate on the /v1 prefix

The contract puts authentication on the route prefix — "all `/v1/*` routes (every
method, including GET)" — not on the three handlers we happen to have implemented.
Auth therefore runs as a raw ASGI middleware ahead of routing, because Starlette
resolves unknown paths and disallowed methods into 404s and 405s before any handler
executes; a check inside `create_review` cannot speak for `PUT /v1/reviews`. Written
before this change, `GET /v1/unknown` and `PUT /v1/reviews` both answered anonymous
callers, which also disclosed which `/v1` routes exist.

## Considered options

`@app.middleware("http")` was rejected: it constructs a `BaseHTTPMiddleware`, which
re-wraps every response in its own streaming shim. SSE is a scored path that already
worked, and there was no reason to put a shim in it. A raw ASGI class passes
authorized traffic through untouched.

A catch-all `/v1/{rest:path}` route was rejected because Starlette matches a wrong
method on an existing path (405) before it reaches a catch-all, so the hole would
have stayed open for exactly the cases that motivated the change.

## Consequences

405 now collapses into 404 throughout the service. The published error taxonomy has
one code for "no such thing" (`not_found`) and none for "wrong method", so emitting
a 405 would mean returning a status with no code entitled to carry it. Every status
the service can produce now maps onto a code the task named.
