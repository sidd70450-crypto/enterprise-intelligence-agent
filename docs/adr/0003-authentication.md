# ADR 0003: Authentication, sessions and authorization

- Status: accepted
- Date: 2026-11-16

## Decision

**Passwords.** Argon2id with the library's default parameters, hashed in a worker
thread so the event loop is not blocked. Hashes are upgraded on the next login if
the parameters change. A login for an unknown email still runs one Argon2
verification, against a dummy hash, so response time does not reveal which
accounts exist. Passwords must be 12 to 128 characters.

**Access tokens.** HS256 JWTs, 15 minutes by default, carrying only the user ID
and organization ID (plus `iss`, `aud`, `typ`, `iat`, `exp`, `jti`). The
accepted algorithm is fixed in code, so `alg: none` and algorithm-confusion
tokens are rejected. The role is not in the token: every request loads the
membership, role and `is_active` flag from the database. Removing a membership or
deactivating a user therefore takes effect on the next request, at the cost of
one indexed query per request.

**Refresh tokens.** 256-bit random strings, stored only as SHA-256 hashes, sent
in an `httpOnly`, `Secure`, `SameSite=Strict` cookie whose path is
`/api/v1/auth`. Every refresh revokes the presented token and issues a successor
in the same *family*. If a token that was already rotated is presented again, the
whole family is revoked and the event is audited: either the token was stolen or
the client replayed it, and both mean the session can no longer be trusted. The
refresh row is locked (`SELECT ... FOR UPDATE`) so two concurrent refreshes of
one token cannot both succeed and fork the session.

Consequence: a client that fires two refreshes at once (for example two browser
tabs) will be logged out. The frontend (M6) must make refresh single-flight. A
grace period for near-simultaneous reuse was considered and rejected because it
also gives an attacker that window.

**Authorization.** Roles are rows with a list of permission strings
(`members:read`, ...), seeded by migration. Routes declare what they need with
`require_permission("members:read")`; missing permission is 403. The organization
is always taken from the verified token, never from the request, and resources in
other organizations return 404, never 403, so their existence is not revealed.

**Rate limits.** Failed logins are counted in Redis per email (default 10 per
15 minutes) and per IP (5 times that). At the limit, logins are refused with 429
even with the right password. Registrations are limited per IP (default 10 per
hour) because each one costs an Argon2 hash. If Redis is down the limits fail
open and a warning is logged: failing closed would lock everyone out during a
Redis outage.

**Errors.** All errors are RFC 9457 problem details with the request ID.
Validation errors never echo submitted values. Unhandled exceptions return a
generic 500 and are logged with the traceback.

## Accepted risks

- **Account lockout by a third party.** Anyone who knows an email can trigger its
  login limit for 15 minutes. Accepted for v0.1; per-device trust or CAPTCHA can
  be added later.
- **Account enumeration through registration.** Registering an existing email
  returns 409. Hiding this would need email verification, which is out of scope
  for v0.1.
- **Client IP behind a proxy.** Rate limits and audit records use the direct peer
  address. Deployment (M7) must configure trusted forwarded headers.
- **Expired refresh tokens accumulate.** A periodic cleanup job arrives with the
  worker in M4.
- **One organization per login.** A user with several memberships is logged into
  the oldest one; switching organizations comes with invitations in v0.2.
