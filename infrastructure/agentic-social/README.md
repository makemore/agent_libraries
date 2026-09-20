# Postiz has moved to the shared business-tools deployment

Use [business-tools](../business-tools/README.md) for the requested shared host.
Its [agentic-social fragment](../business-tools/agentic-social/README.md) runs
Postiz alongside Mautic, Actual Budget, Invoice Ninja and Grafana/Prometheus.

This directory is now a documentation redirect only. The superseded standalone
Terraform, runtime and template files were removed during the approved cleanup.
Do not recreate a second deployment for `social.makemoredigital.com` here.

Source cleanup does not migrate or destroy cloud resources. If the old standalone
stack was applied independently, review its data, credentials, DNS and remote state
before switching; none of those were changed by this cleanup.