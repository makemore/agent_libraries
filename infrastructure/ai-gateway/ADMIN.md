# Google-protected browser administration

The optional browser entrypoint uses a separate hostname and a Google external
Application Load Balancer with Identity-Aware Proxy (IAP). The public inference
hostname, virtual-key policy, private Bifrost login and IAP SSH fallback are unchanged.

## Architecture and security

- Enable explicitly with `admin_domain` and `admin_members` in deployment variables.
  Both are absent/empty by default. The admin hostname must differ from the API hostname.
- Only explicit `user:` identities are supported. IAM grants the backend-scoped
  `roles/iap.httpsResourceAccessor`; the same exact emails form a signed-token allowlist.
  Group/domain/public/service-account grants are rejected rather than weakened to allow-all.
- Google-managed OAuth limits browser sign-in to the project's Workspace organization.
  No OAuth client secret or user password is added to OpenTofu state.
- The dedicated global IP accepts HTTPS 443 only, using a Google-managed certificate
  and MODERN TLS policy with TLS 1.2 minimum. No new public HTTP listener is created.
- Only Google's GFE/health-check ranges can reach the separate VM listener, TCP 8081.
  Bifrost remains on loopback 8080; the verifier remains on loopback 9091.
- Caddy checks the exact admin Host and calls the verifier before every admin request.
  Missing/forged/expired tokens, wrong issuer/audience/user and unavailable verification
  fail closed. Only the dedicated GET `/_iap_health` returns a static health response
  without a token; it never forwards into Bifrost.
- The auth subrequest uses `uri /verify?` to clear inherited query parameters.
  A bare `/verify` preserves the dashboard's `?from_db=false` and is rejected by the
  verifier's exact-target check. The original query still reaches Bifrost unchanged;
  do not strip it from the main request or relax the verifier's route/authentication.
- The verifier uses existing Ubuntu 24.04 system PyJWT/cryptography packages, ES256,
  Google's HTTPS public keys, bounded caching and the numeric backend-service audience.
  It does not follow token-supplied key URLs. No custom cryptography is implemented.
- A sandboxed DynamicUser service reads only its nonsecret code/config under
  `/usr/local/lib/ai-gateway`. It has no access to the bootstrap credentials or databases.
- Bifrost's own session authentication is retained; IAP does not silently log the user
  in as admin. Google identity headers are stripped before forwarding to Bifrost.
- Load-balancer access logging is disabled. Caddy error logs remove request headers and
  URI; the verifier never logs assertions, cookies, identities or request contents.

## Provisioning and updates

Set an explicit admin hostname and allowed user identities, review the plan, then apply.
An empty instance group and backend are created before VM metadata is rendered; a
separate membership resource attaches the VM afterward. This avoids the cyclic dependency
between the numeric JWT audience and the VM. Only the group's `instances` field is
ignored by the group resource, because the membership resource owns it.

After apply, rerun `sudo google_metadata_script_runner startup` through the existing
IAP SSH access. This briefly restarts the gateway and preserves persisted Bifrost choices.
The runtime checks for the already-installed JWT libraries; it does not automatically
install dependencies or disable verification if they are absent.

When DNS is external, add an A record for the **admin hostname** using `admin_static_ip`.
Do not repoint the existing API record. Google certificate issuance requires that record
to resolve to the new load balancer. The admin endpoint is not ready until the certificate
is ACTIVE, the backend is healthy and access-boundary checks pass.

If `dns_managed_zone` explicitly selects an existing Cloud DNS zone, OpenTofu manages
the corresponding admin A record there. Do not opt into this for a GoDaddy-hosted zone.

## Login and fallback

Open the HTTPS admin URL, authenticate with an allowed Workspace account, then use the
existing Bifrost admin credentials. Google account MFA policy still applies. IAP's gate
does not automatically enforce new device-trust or reauthentication policies.

Keep the private IAP SSH tunnel as a recovery route. It intentionally bypasses the
browser gate for authorized VM operators, but still requires Bifrost authentication.
Never expose port 8080 or 9091 publicly to troubleshoot a login failure.

## Verification

Run the Python suite and the isolated mocked OpenTofu plans described in README.md.
The new tests cover disabled defaults, explicit single-user setup, rejected identities,
signature/claim/cache failures and runtime privacy. On Docker Desktop, also run:

<augment_code_snippet mode="EXCERPT">
````sh
GATEWAY_CONTAINER_TESTS=1 GATEWAY_ADMIN_CONTAINER_TESTS=1 \
  .venv/bin/python -m unittest discover \
  -s infrastructure/ai-gateway/tests -p test_containers.py -v
````
</augment_code_snippet>

The named admin smoke test uses ephemeral signing keys and a test-only host bridge to
reach the real verifier. It checks valid/forged assertions, retained Bifrost auth, wrong
Host and verifier-down denial, including query-bearing requests. It also completes an
actual Bifrost login, reads JSON config with the Secure/HTTPOnly session cookie and the
dashboard's query string, then verifies logout invalidation. No production key URL,
user data or provider API is used.

On the deployed gateway, verify:
- Backend health and certificate ACTIVE; unauthenticated HTTPS redirects to Google.
- Direct internet access to 8081/8080/9091 is blocked; direct VM admin requests without
  a valid Google assertion fail even with a Bifrost password or unsigned identity headers.
- The public inference hostname still blocks admin/model-list routes and requires keys.
- An allowed user can complete Google and Bifrost login; an ungranted user is denied.
- Google's `secure_token_test` cases fail at the verifier, not open the app.

Interactive account-login checks require the operator's browser. Do not record browser
cookies, OAuth callbacks, assertions or credentials in terminal output or chat.

## Costs and operations

Basic IAP on GCP-hosted applications has no separate fee. Load-balancer forwarding-rule
and traffic-processing charges apply. At setup, this project had two global rules;
adding this third stays in the current first-five-global-rule pricing band. Review
current billing/pricing rather than assuming that all load balancing is free.

Adding/removing an admin updates both backend IAM and the verifier allowlist after the
runtime rollout. Removing someone from the signed allowlist provides a second denial
boundary even if broader inherited IAM grants exist. Preserve the tunnel for recovery.

Sources: [IAP setup](https://cloud.google.com/iap/docs/tutorial-gce),
[signed headers](https://cloud.google.com/iap/docs/signed-headers-howto),
[managed OAuth](https://cloud.google.com/iap/docs/deprecations/migrate-oauth-client),
[IAP pricing](https://cloud.google.com/iap/pricing),
[network pricing](https://cloud.google.com/vpc/network-pricing).