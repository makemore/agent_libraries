# Pipeboard: hosted paid-ad management

**Decision:** use Pipeboard's paid hosted service for Google Ads and Meta Ads.
This is an alternative to operating our own ads-management server, **not a sixth
service on the business-tools VM**. No Compose container, VM, Terraform state,
DNS record or self-hosted provider credentials are required here. Postiz remains
the shared stack's social-publishing application; it is not replaced or migrated.

## Current status: documented, not connected

This directory contains an operator checklist and a token-free, inert MCP example.
It does **not** install a client, configure a global/repository-active MCP file,
create or purchase an account, log in, connect ad accounts or make ad API calls.
Existing account, plan, payment and OAuth status have not been inspected.

| State | Evidence required |
| --- | --- |
| Configured | The chosen client accepts the two remote entries in its own supported format. An example file alone does not meet this. |
| Authenticated | The human completes client OAuth to the intended Pipeboard identity. This does not prove ad-account access. |
| Connected | Pipeboard has the approved Google/Meta account connections, and a scoped read succeeds for each intended account ID. Tool discovery alone is insufficient. |
| Write-authorized | A separate, specific approval covers the exact proposed mutation and spending constraints below. Connection is not write approval. |

## Human setup checklist

1. **Account and commercial approval.** At [Pipeboard](https://pipeboard.co/), use
   the intended business identity or ask before creating an account. Review current
   [pricing](https://pipeboard.co/pricing), account limits, team access, data handling
   and renewal terms. Obtain approval for the exact plan, price/currency, billing
   interval and payment before subscribing. The human enters payment information
   privately; never copy it into chat. Pipeboard subscription fees, client-plan
   fees and Google/Meta advertising spend are separate costs.
2. **Connect the ad providers in Pipeboard.** The authorized human signs into
   Google/Meta, checks the business/manager relationship and chooses only approved
   accounts/assets and scopes. Record the approved Google customer IDs (including
   manager context where needed) and Meta ad account IDs in the private operating
   record. Do not infer permission from whichever account appears first. Confirm
   provider billing/currency and account eligibility without creating a campaign.
3. **Choose an OAuth-capable remote MCP client.** Confirm the installed client and
   its plan support remote HTTP MCP and the provider's OAuth flow. Add only the two
   endpoints below using that client's UI/config format. The human completes its
   connection/consent flow; this authenticates the client separately from linking
   Google/Meta to Pipeboard. No token in a URL, header example, argv or repository.
4. **Verify reads only.** With approval to read the named accounts, discover tools,
   inspect their schemas and request a small account/campaign summary for a bounded
   date range. Confirm ID, currency/timezone and result count match the intended
   account. Keep raw responses, personal data and signed/preview URLs private;
   report only approved identifiers, counts and success/failure. Do not create a
   test campaign or email report to prove connectivity.
5. **Record completion honestly.** Record client/version, setup date, approved
   account IDs, successful read checks and any pending human steps without secrets.
   Leave writes unapproved until the next section's conditions are met. If OAuth,
   plan entitlement or account access fails, stop and resolve that layer; do not
   widen scopes, switch billing accounts or buy another plan automatically.

## Remote MCP endpoints and inert example

The provider's [Claude guide](https://pipeboard.co/guides/claude) and
[Claude Code guide](https://pipeboard.co/guides/claude-code) document these
OAuth connection endpoints (checked 2026-09-20):

| Provider | Remote HTTP MCP endpoint |
| --- | --- |
| Google Ads | `https://google-ads.mcp.pipeboard.co/` |
| Meta Ads | `https://meta-ads.mcp.pipeboard.co/` |

[mcp.example.json](mcp.example.json) contains **only these two endpoints**, using
a Claude Code-style `mcpServers`/`type: http` shape. It is an **EXAMPLE**, not an
auto-active repository config and not a universal MCP configuration schema.
Other clients use different fields or connector UIs; follow their current docs
and complete OAuth through the client. Do not copy it over existing configuration
or install bridges without approval. If the client lacks the required OAuth/HTTP
support, choose a supported client rather than embedding tokens as a workaround.

The CLI README also publishes path-based MCP addresses on `mcp.pipeboard.co`.
This example deliberately follows the provider's client OAuth guides above; it
does not claim every documented address/authentication mode is interchangeable.
No third provider or aggregate connector is enabled here.

## Mutation and spending guardrails

Initial operation is **read-only by policy, not an enforced MCP permission**.
Pipeboard tools may create/update campaigns and perform broader mutations. This
example does not restrict OAuth scopes or install an authorization proxy. Use
least-privilege provider roles and per-tool client approval controls where actually
supported and verified; never describe an agent instruction as a hard security limit.

Before **any write**, obtain explicit approval naming the provider, exact account
IDs, target campaign/ad set/ad IDs (or proposed new objects), action and payload,
budget amount/type (daily versus lifetime), currency, start/end dates, timezone
and duration. Include applicable existing budgets for non-budget edits; document
zero incremental spend where relevant rather than assuming it. Confirm API units
(for example micros/minor units) and shared-budget effects against the discovered
schema. Approval must cover targeting, creative/destination, status and any spend
increase; no open-ended “optimize my ads” permission.

Creating paused objects, uploading audiences/assets, scheduling reports, pausing,
deleting, changing bids/budgets and generic mutate tools are all writes. Do not
auto-enable campaigns or use write calls as smoke tests. After an approved change,
read back the result, compare it with the approval and keep a minimal private audit
record. A planned budget is not a guaranteed billing ceiling; check platform rules
and arrange human monitoring/stop conditions. Unexpected accounts or values mean stop.

## Optional CLI: separate installation approval

MCP does not require the Pipeboard CLI. Its official
[README source](https://github.com/pipeboard-co/pipeboard-cli/blob/main/README.md)
documents Homebrew installation and interactive browser OAuth. Installing it is a
**new dependency**: obtain confirmation for the target machine/package first;
no installation or global configuration has been performed by this setup.

Only after that approval, the documented Homebrew command is:

<augment_code_snippet mode="EXCERPT">
````sh
brew install pipeboard-co/tap/pipeboard
````
</augment_code_snippet>

After a separately approved human login, these documented commands provide OAuth
and command discovery; they are instructions, not commands executed here:

<augment_code_snippet mode="EXCERPT">
````sh
pipeboard login
pipeboard google-ads --help
pipeboard meta-ads --help
````
</augment_code_snippet>

Run login in the human's private terminal/browser, not a captured agent session;
do not paste authentication URLs or session output. Avoid upstream examples that
put tokens in arguments or print configuration. Keep client credential storage
outside the repository. If automation later requires non-interactive credentials,
review a supported secret-store integration separately—none is configured here.

To disconnect, use the client's connection controls and revoke the corresponding
Pipeboard/provider authorization through the human's account settings. Removing an
example/config entry alone is not credential revocation or subscription cancellation.